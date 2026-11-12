/**
 * daemon 的 unix socket 线协议：一行一个 JSON。
 *
 * 请求：`{"id":N,"method":"...","params":{...}}`
 * 应答：`{"id":N,"ok":true,"result":{...}}` / `{"id":N,"ok":false,"error":{...}}`
 * 流式：`{"id":N,"stream":true,"event":"assistant_delta"|"reasoning_delta"|"trace"|"keepalive"|"final",...}`
 *
 * 取消靠断开连接：daemon 侧有补丁，socket 一断就 cancel 回合
 * （`patch_turn_cancel.py`，见 ops.md §7-5）。
 */
import net from "node:net";

export class MbotError extends Error {
	constructor(message, { code = "MBOT_ERROR", info } = {}) {
		super(message);
		this.name = "MbotError";
		this.code = code;
		if (info !== undefined) this.info = info;
	}
}

export function abortError() {
	const error = new Error("This operation was aborted");
	error.name = "AbortError";
	return error;
}

/** 把 socket 的字节流切成行，并提供「下一次读取」。只有单读者，超时即毁连接。 */
class LineReader {
	#buffer = "";
	#lines = [];
	#wake = null;
	#error = null;
	#eof = false;

	push(text) {
		this.#buffer += text;
		let cut;
		while ((cut = this.#buffer.indexOf("\n")) >= 0) {
			const line = this.#buffer.slice(0, cut).trim();
			this.#buffer = this.#buffer.slice(cut + 1);
			if (line) this.#lines.push(line);
		}
		this.#signal();
	}

	end() {
		this.#eof = true;
		this.#signal();
	}

	fail(error) {
		this.#error ??= error;
		this.#signal();
	}

	#signal() {
		const wake = this.#wake;
		if (wake) {
			this.#wake = null;
			wake();
		}
	}

	/** 取下一行；EOF 返回 null；出错抛出。 */
	async next() {
		while (true) {
			if (this.#error) throw this.#error;
			const line = this.#lines.shift();
			if (line !== undefined) return line;
			if (this.#eof) return null;
			await new Promise((resolve) => {
				this.#wake = resolve;
			});
		}
	}

	/** 带超时的等待：超时视为链路死亡（调用方随后会 close），避免半死连接一直挂着。 */
	async nextWithin(timeoutMs, onTimeout) {
		if (this.#lines.length > 0 || this.#error || this.#eof) return this.next();
		let timer;
		const timeout = new Promise((_, reject) => {
			timer = setTimeout(() => reject(onTimeout()), timeoutMs);
			timer.unref?.();
		});
		try {
			return await Promise.race([this.next(), timeout]);
		} finally {
			clearTimeout(timer);
		}
	}
}

/** 一条到 daemon 的连接。每回合一条，用完就关（bridge 也是这个模型）。 */
export class MbotLink {
	#socket;
	#reader = new LineReader();
	#nextId = 1;
	#closed = false;

	constructor(socketPath) {
		this.socketPath = socketPath;
	}

	static open(socketPath, { timeoutMs = 10_000 } = {}) {
		return new Promise((resolve, reject) => {
			const socket = net.createConnection({ path: socketPath });
			const link = new MbotLink(socketPath);
			link.#socket = socket;
			const timer = setTimeout(() => {
				socket.destroy();
				reject(new MbotError(`连接 daemon 超时：${socketPath}`, { code: "MBOT_CONNECT_TIMEOUT" }));
			}, timeoutMs);
			timer.unref?.();
			socket.setEncoding("utf8");
			socket.on("data", (text) => link.#reader.push(text));
			socket.on("end", () => link.#reader.end());
			socket.on("close", () => {
				link.#closed = true;
				link.#reader.end();
			});
			socket.on("error", (error) => {
				link.#closed = true;
				link.#reader.fail(new MbotError(`daemon 连接出错：${error.message}`, { code: "MBOT_SOCKET_ERROR" }));
			});
			socket.once("connect", () => {
				clearTimeout(timer);
				resolve(link);
			});
			socket.once("error", (error) => {
				clearTimeout(timer);
				reject(
					new MbotError(`连不上 daemon（${socketPath}）：${error.message}`, { code: "MBOT_CONNECT_FAILED" }),
				);
			});
		});
	}

	get closed() {
		return this.#closed;
	}

	#write(frame) {
		if (this.#closed) throw new MbotError("daemon 连接已关闭", { code: "MBOT_LINK_CLOSED" });
		this.#socket.write(`${JSON.stringify(frame)}\n`);
	}

	/** 一次请求/应答。 */
	async request(method, params = {}, { timeoutMs = 30_000 } = {}) {
		const id = this.#nextId++;
		this.#write({ id, method, params });
		const deadline = Date.now() + timeoutMs;
		while (true) {
			const line = await this.#reader.nextWithin(Math.max(50, deadline - Date.now()), () => {
				this.close();
				return new MbotError(`${method} 超时（${timeoutMs}ms）`, { code: "MBOT_TIMEOUT" });
			});
			if (line === null) throw new MbotError(`${method} 期间 daemon 断流`, { code: "MBOT_STREAM_CLOSED" });
			let frame;
			try {
				frame = JSON.parse(line);
			} catch {
				continue;
			}
			if (frame.stream === true) continue; /* 不该出现，跳过 */
			if (frame.id !== id) continue;
			if (frame.ok === false) {
				throw new MbotError(String(frame.error?.message ?? `${method} 失败`), {
					code: String(frame.error?.code ?? "MBOT_CALL_FAILED"),
					info: frame.error,
				});
			}
			return frame.result ?? {};
		}
	}

	/**
	 * 流式方法：逐个产出 `{"stream":true,...}` 帧，直到 `event:"final"`。
	 * 非流式的最终应答也归一到 `{stream:false, result}`。
	 * `signal` 一 abort 就毁连接（daemon 会 cancel 回合）。
	 */
	async *stream(method, params = {}, { signal, idleMs = 180_000, timeoutMs = 60_000, isProgress = () => true } = {}) {
		const id = this.#nextId++;
		this.#write({ id, method, params });
		const onAbort = () => this.close(abortError());
		signal?.addEventListener("abort", onAbort, { once: true });
		let sawFrame = false;
		/*
		 * 心跳不算「有进展」：daemon 每 15 秒发一次 keepalive，如果拿它刷新空闲计时，
		 * 模型卡住时回合会永远不结束（实测挂到 668 秒，用户手动中止）。
		 * 这里只在 isProgress(frame) 为真时重新计时。
		 */
		let lastProgressAt = Date.now();
		try {
			while (true) {
				if (signal?.aborted) throw abortError();
				/* idleMs 允许传函数：等模型和等工具的容忍度不一样（见 turn.js） */
				const budget = typeof idleMs === "function" ? Number(idleMs()) : Number(idleMs);
				const idleLeft = budget - (Date.now() - lastProgressAt);
				/* 心跳还在陆续到达也不能延长时间：到点就判卡死，别等下一帧 */
				if (sawFrame && idleLeft <= 0) {
					this.close();
					throw new MbotError(`daemon ${Math.round(budget / 1000)} 秒没有任何推进（心跳不算），判定卡死`, {
						code: "MBOT_STALLED",
					});
				}
				const line = await this.#reader.nextWithin(sawFrame ? Math.max(50, idleLeft) : timeoutMs, () => {
					this.close();
					return new MbotError(
						sawFrame
							? `daemon ${Math.round(budget / 1000)} 秒没有任何推进（心跳不算），判定卡死`
							: `${method} 超时（${timeoutMs}ms）`,
						{ code: sawFrame ? "MBOT_STALLED" : "MBOT_TIMEOUT" },
					);
				});
				if (line === null) {
					throw new MbotError("daemon 没有发收尾事件就断流了", { code: "MBOT_STREAM_CLOSED" });
				}
				let frame;
				try {
					frame = JSON.parse(line);
				} catch {
					continue;
				}
				if (frame.stream === true) {
					sawFrame = true;
					if (isProgress(frame)) lastProgressAt = Date.now();
					yield frame;
					if (frame.event === "final") return;
					continue;
				}
				if (frame.id !== id) continue;
				if (frame.ok === false) {
					throw new MbotError(String(frame.error?.message ?? `${method} 失败`), {
						code: String(frame.error?.code ?? "MBOT_CALL_FAILED"),
						info: frame.error,
					});
				}
				/* 纯 ack（ok:true 但没有 result）→ 继续等流 */
				if (frame.result === undefined) continue;
				yield { stream: false, ...frame };
				return;
			}
		} finally {
			signal?.removeEventListener("abort", onAbort);
		}
	}

	close(reason) {
		if (this.#closed) return;
		this.#closed = true;
		this.#reader.fail(reason ?? new MbotError("连接已关闭", { code: "MBOT_LINK_CLOSED" }));
		try {
			this.#socket.destroy();
		} catch {
			/* 已经坏了 */
		}
	}
}
