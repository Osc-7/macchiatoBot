/**
 * DSH 客户端插件：右侧栏「终端」标签页（原生 xterm.js + WebSocket + PTY）。
 *
 * 浏览器端模块格式与 DSH 自家客户端包一致：
 *   window.__ModuleLoader__.load({ id, factory })   ← 只注册工厂，不产生副作用
 * 依赖的模块由 package.json 的 `dsh.client.inject` 声明。
 *
 * 三块：
 *   - 资源：/xterm/{xterm.js,xterm.css,addon-fit.js}（nginx 静态目录，UMD 构建）
 *   - 传输：/pty（WebSocket，lab-pty.service 的 PTY 桥，JSON 帧）
 *   - 外观：VSCode Dark+ 主题，和 DSH 深色界面同一套色板
 */
window.__ModuleLoader__.load({
	id: "@lab/dsh-agent-mbot",
	factory: (require) => {
		var module = { exports: {} };
		var exports = module.exports;
		Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });
		const react = require("react");
		const React = react;
		const jsxRuntime = require("react/jsx-runtime");

		const TERMINAL_ID = "labTerminal";
		const NS = "labTerminal";

		const zh = {
			"type.label": "终端",
			"guide.title": "终端",
			"guide.description": "在服务器上开一个交互 shell（xterm + PTY）",
			"connecting": "正在连接 shell…",
			"closed": "连接已断开",
			"reconnect": "重新连接",
			"failed": "终端资源加载失败",
		};
		const en = {
			"type.label": "Terminal",
			"guide.title": "Terminal",
			"guide.description": "Open an interactive shell on the host (xterm + PTY)",
			"connecting": "Connecting to the shell…",
			"closed": "Disconnected",
			"reconnect": "Reconnect",
			"failed": "Failed to load terminal assets",
		};

		/** VSCode Dark+ 十六色 + 底色。 */
		const VSC_THEME = {
			background: "#1e1e1e",
			foreground: "#d4d4d4",
			cursor: "#aeafad",
			cursorAccent: "#1e1e1e",
			selectionBackground: "#264f78",
			black: "#000000",
			red: "#cd3131",
			green: "#0dbc79",
			yellow: "#e5e510",
			blue: "#2472c8",
			magenta: "#bc3fbc",
			cyan: "#11a8cd",
			white: "#e5e5e5",
			brightBlack: "#666666",
			brightRed: "#f14c4c",
			brightGreen: "#23d18b",
			brightYellow: "#f5f543",
			brightBlue: "#3b8eea",
			brightMagenta: "#d670d6",
			brightCyan: "#29b8db",
			brightWhite: "#e5e5e5",
		};

		const FONT_STACK =
			'Menlo, Monaco, "Cascadia Mono", "Segoe UI Mono", "Roboto Mono", "Courier New", monospace';

		/** 加载 xterm 的 UMD 资源（只加载一次，多开标签页共享）。 */
		let assetsPromise;
		function loadAssets() {
			if (assetsPromise) return assetsPromise;
			assetsPromise = new Promise((resolve, reject) => {
				const link = document.createElement("link");
				link.rel = "stylesheet";
				link.href = "/xterm/xterm.css";
				document.head.appendChild(link);
				const first = document.createElement("script");
				first.src = "/xterm/xterm.js";
				first.onload = () => {
					const second = document.createElement("script");
					second.src = "/xterm/addon-fit.js";
					second.onload = () => resolve();
					second.onerror = () => reject(new Error("addon-fit"));
					document.head.appendChild(second);
				};
				first.onerror = () => reject(new Error("xterm"));
				document.head.appendChild(first);
			});
			return assetsPromise;
		}

		function TerminalGlyph() {
			return jsxRuntime.jsx("span", {
				style: { fontFamily: "monospace", fontSize: 13, lineHeight: "16px" },
				children: ">_",
			});
		}

		/** 标签页主体：一个铺满面板的 xterm。 */
		function TerminalBody() {
			const hostRef = react.useRef(null);
			const [status, setStatus] = react.useState("connecting");
			const socketRef = react.useRef(null);
			const reconnectRef = react.useRef(null);

			react.useEffect(() => {
				let disposed = false;
				let term;
				let fit;
				let observer;

				loadAssets()
					.then(() => {
						if (disposed || !hostRef.current) return;
						term = new window.Terminal({
							theme: VSC_THEME,
							fontFamily: FONT_STACK,
							fontSize: 13,
							cursorBlink: true,
							scrollback: 8000,
							convertEol: false,
						});
						fit = new window.FitAddon.FitAddon();
						term.loadAddon(fit);
						term.open(hostRef.current);

						const sendSize = () => {
							const socket = socketRef.current;
							if (socket && socket.readyState === 1) {
								socket.send(JSON.stringify({ t: "r", c: term.cols, r: term.rows }));
							}
						};
						const refit = () => {
							try {
								fit.fit();
							} catch {
								/* 面板还没布局好 */
							}
						};

						const connect = () => {
							if (disposed) return;
							setStatus("connecting");
							const proto = location.protocol === "https:" ? "wss:" : "ws:";
							const socket = new WebSocket(`${proto}//${location.host}/pty`);
							socketRef.current = socket;
							socket.onopen = () => {
								setStatus("open");
								refit();
								sendSize();
								/* 面板动画结束后再对齐一次 */
								setTimeout(() => {
									refit();
									sendSize();
								}, 150);
							};
							socket.onmessage = (event) => {
								try {
									const message = JSON.parse(event.data);
									if (message.t === "o") term.write(message.d);
								} catch {
									/* 非 JSON 帧忽略 */
								}
							};
							socket.onclose = () => {
								if (disposed) return;
								setStatus("closed");
								term.write("\r\n\x1b[90m[连接已断开]\x1b[0m\r\n");
							};
							socket.onerror = () => {
								if (!disposed) setStatus("closed");
							};
						};

						reconnectRef.current = () => {
							try {
								socketRef.current?.close();
							} catch {
								/* ignore */
							}
							term.reset();
							connect();
						};

						term.onData((data) => {
							const socket = socketRef.current;
							if (socket && socket.readyState === 1) socket.send(JSON.stringify({ t: "i", d: data }));
						});
						term.onResize(sendSize);

						connect();
						refit();

						observer = new ResizeObserver(() => {
							refit();
							sendSize();
						});
						observer.observe(hostRef.current);
					})
					.catch(() => {
						if (!disposed) setStatus("failed");
					});

				return () => {
					disposed = true;
					try {
						observer?.disconnect();
					} catch {
						/* ignore */
					}
					try {
						socketRef.current?.close();
					} catch {
						/* ignore */
					}
					try {
						term?.dispose();
					} catch {
						/* ignore */
					}
					socketRef.current = null;
					reconnectRef.current = null;
				};
			}, []);

			const overlay =
				status === "open"
					? null
					: jsxRuntime.jsx("div", {
							style: {
								position: "absolute",
								left: 0,
								right: 0,
								bottom: 0,
								padding: "4px 10px",
								fontSize: 12,
								color: "#d4d4d4",
								background: "#252526",
								borderTop: "1px solid #333",
								display: "flex",
								gap: 10,
								alignItems: "center",
							},
							children:
								status === "closed"
									? [
											jsxRuntime.jsx("span", { children: "连接已断开" }, "t"),
											jsxRuntime.jsx(
												"button",
												{
													onClick: () => reconnectRef.current?.(),
													style: {
														background: "#0e639c",
														color: "#fff",
														border: 0,
														borderRadius: 3,
														padding: "2px 10px",
														cursor: "pointer",
														fontSize: 12,
													},
													children: "重新连接",
												},
												"b",
											),
										]
									: jsxRuntime.jsx("span", {
											children: status === "failed" ? "终端资源加载失败（/xterm/）" : "正在连接 shell…",
										}),
						});

			return jsxRuntime.jsxs("div", {
				style: { position: "relative", width: "100%", height: "100%", background: VSC_THEME.background, minHeight: 0 },
				children: [
					jsxRuntime.jsx("div", {
						ref: hostRef,
						style: { position: "absolute", inset: 0, padding: "6px 8px", boxSizing: "border-box" },
					}),
					overlay,
				],
			});
		}

		/** 标签页标题（页签上的那行字）。 */
		function TerminalTitle() {
			return jsxRuntime.jsx("span", { children: "终端" });
		}

		function terminalDefinition(t) {
			return {
				id: TERMINAL_ID,
				kind: TERMINAL_ID,
				priority: "builtin",
				title: () => t("type.label"),
				guide: [
					{
						order: 30,
						title: () => t("guide.title"),
						description: () => t("guide.description"),
						icon: TerminalGlyph,
					},
				],
			};
		}


		/* ================================================================
		 * 工作区标签页：文件树 + CodeMirror 编辑器 + PDF/图片查看
		 * 数据全部走 /fs 的 WebSocket（与终端共用 lab-bridge 进程）
		 * ================================================================ */

		const WORKSPACE_ID = "labWorkspace";

		const wsText = {
			"type.label": "工作区",
			"guide.title": "工作区",
			"guide.description": "浏览/编辑机器上的文件，PDF 直接看（WebSocket）",
			"pick": "选择机器与项目",
			"saved": "已保存",
			"save": "保存",
			"dirty": "有未保存改动",
			"binary": "二进制文件，已切换到预览",
			"empty": "从左边选个文件",
			"loading": "读取中…",
			"error": "出错了",
			"reload": "重新读取",
			"machine": "机器",
			"proj": "项目",
			"file": "文件",
		};
		const wsEnglish = {
			"type.label": "Workspace",
			"guide.title": "Workspace",
			"guide.description": "Browse and edit files on the machine, view PDFs (WebSocket)",
			"pick": "Pick a machine and project",
			"saved": "Saved",
			"save": "Save",
			"dirty": "Unsaved changes",
			"binary": "Binary file, switched to preview",
			"empty": "Pick a file on the left",
			"loading": "Loading…",
			"error": "Error",
			"reload": "Reload",
			"machine": "Machine",
			"proj": "Project",
			"file": "File",
		};

		/** CodeMirror 资源：脚本只注入一次，模式按需加载。 */
		let cmPromise;
		function loadCodemirror() {
			if (cmPromise) return cmPromise;
			cmPromise = new Promise((resolve, reject) => {
				const style = document.createElement("link");
				style.rel = "stylesheet";
				style.href = "/editor/codemirror.css";
				document.head.appendChild(style);
				const theme = document.createElement("link");
				theme.rel = "stylesheet";
				theme.href = "/editor/theme/material-darker.css";
				document.head.appendChild(theme);
				const script = document.createElement("script");
				script.src = "/editor/codemirror.js";
				script.onload = () => resolve(window.CodeMirror);
				script.onerror = () => reject(new Error("CodeMirror 加载失败"));
				document.head.appendChild(script);
			});
			return cmPromise;
		}

		const MODE_BY_EXT = {
			js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript", ts: "javascript", tsx: "javascript",
			py: "python", pyi: "python",
			c: "clike", h: "clike", cc: "clike", cpp: "clike", hpp: "clike", cu: "clike", cuh: "clike",
			sh: "shell", bash: "shell", zsh: "shell",
			md: "markdown", markdown: "markdown",
			yaml: "yaml", yml: "yaml",
			json: "javascript",
			html: "htmlmixed", htm: "htmlmixed", xml: "xml",
			css: "css", scss: "css",
			coffee: "coffeescript",
			txt: null, log: null, cfg: null, ini: null,
		};

		function modeOf(path) {
			const ext = String(path).split(".").pop()?.toLowerCase() ?? "";
			return MODE_BY_EXT[ext] ?? null;
		}

		const loadedModes = new Set();
		function loadMode(name) {
			if (!name || loadedModes.has(name)) return Promise.resolve();
			loadedModes.add(name);
			return new Promise((resolve) => {
				const script = document.createElement("script");
				script.src = `/editor/mode/${name}.js`;
				script.onload = () => resolve();
				script.onerror = () => resolve();
				document.head.appendChild(script);
			});
		}

		const IMAGE_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico"]);
		function isImage(path) {
			return IMAGE_EXT.has(String(path).split(".").pop()?.toLowerCase() ?? "");
		}
		function isPdf(path) {
			return String(path).split(".").pop()?.toLowerCase() === "pdf";
		}
		function joinPath(base, name) {
			if (base.length === 0) return name;
			return `${base.replace(/\/+$/, "")}/${name}`;
		}
		function parentPath(path) {
			if (path.length === 0) return null;
			const cut = path.lastIndexOf("/");
			return cut < 0 ? "" : path.slice(0, cut);
		}

		/** 一次 /fs 请求/应答，带超时。 */
		function wsRequest(socket, frame, expect, timeoutMs = 30000) {
			return new Promise((resolve, reject) => {
				const onMessage = (event) => {
					let message;
					try {
						message = JSON.parse(event.data);
					} catch {
						return;
					}
					if (message.t === "err") {
						cleanup();
						reject(new Error(message.message || message.code || "ws err"));
						return;
					}
					if (message.t !== expect) return;
					cleanup();
					resolve(message);
				};
				const timer = setTimeout(() => {
					cleanup();
					reject(new Error("WebSocket 超时"));
				}, timeoutMs);
				const cleanup = () => {
					clearTimeout(timer);
					socket.removeEventListener("message", onMessage);
				};
				socket.addEventListener("message", onMessage);
				socket.send(JSON.stringify(frame));
			});
		}

		function WorkspaceBody({ sessionId }) {
			const [status, setStatus] = React.useState("connecting");
			const [error, setError] = React.useState("");
			const [catalog, setCatalog] = React.useState(null);
			const [sel, setSel] = React.useState(null);
			const [root, setRoot] = React.useState("");
			const [listings, setListings] = React.useState({});
			const [expanded, setExpanded] = React.useState(() => new Set([""]));
			const [dirPath, setDirPath] = React.useState("");
			const [openFile, setOpenFile] = React.useState(null); /* {path, kind:'text'|'pdf'|'image', text, url, version} */
			const [dirty, setDirty] = React.useState(false);
			const [savedAt, setSavedAt] = React.useState(0);
			const socketRef = React.useRef(null);
			const editorHostRef = React.useRef(null);
			const editorRef = React.useRef(null);
			const blobUrlRef = React.useRef(null);
			const listRef = React.useRef(null);

			/* 建立 WS 连接 */
			React.useEffect(() => {
				const proto = location.protocol === "https:" ? "wss:" : "ws:";
				const socket = new WebSocket(`${proto}//${location.host}/fs`);
				socketRef.current = socket;
				/** 列一个目录并写进 state（onopen 与展开/刷新共用）。 */
				const listInto = async (target, path) => {
					const result = await wsRequest(target, { t: "ls", path }, "ls");
					setListings((previous) => ({ ...previous, [path]: result.entries }));
					return result.entries;
				};
				listRef.current = listInto;
				socket.onopen = async () => {
					setStatus("open");
					try {
						const catalogResult = await wsRequest(socket, { t: "catalog" }, "catalog");
						setCatalog(catalogResult);
						const state = await wsRequest(socket, { t: "state", sessionId }, "state");
						setSel(state.sel);
						setRoot(state.root);
						/* 直接在这里加载根目录：不依赖 React effect 的时序 */
						if (state.sel) await listInto(socket, "");
					} catch (failure) {
						setError(String(failure.message || failure));
					}
				};
				socket.onclose = () => setStatus("closed");
				socket.onerror = () => setStatus("closed");
				return () => {
					try {
						socket.close();
					} catch {
						/* ignore */
					}
					socketRef.current = null;
				};
			}, [sessionId]);

			const loadDir = React.useCallback(async (path) => {
				const socket = socketRef.current;
				if (!socket || socket.readyState !== 1 || !listRef.current) return;
				try {
					await listRef.current(socket, path);
				} catch (failure) {
					setError(String(failure.message || failure));
				}
			}, []);

			const switchSelection = async (machine, proj) => {
				const socket = socketRef.current;
				if (!socket || socket.readyState !== 1) return;
				try {
					const result = await wsRequest(socket, { t: "set", sessionId, machine, proj, dir: "" }, "state");
					setSel(result.sel);
					setRoot(result.root);
					setListings({});
					setExpanded(new Set([""]));
					setDirPath("");
					setOpenFile(null);
					clearEditor();
					await loadDir("");
				} catch (failure) {
					setError(String(failure.message || failure));
				}
			};

			const clearEditor = () => {
				if (editorRef.current) {
					try {
						editorRef.current.toTextArea?.();
					} catch {
						/* ignore */
					}
					editorRef.current = null;
				}
				if (editorHostRef.current) editorHostRef.current.innerHTML = "";
				if (blobUrlRef.current) {
					URL.revokeObjectURL(blobUrlRef.current);
					blobUrlRef.current = null;
				}
			};

			const openPath = async (path) => {
				const socket = socketRef.current;
				if (!socket || socket.readyState !== 1) return;
				setError("");
				try {
					if (isPdf(path) || isImage(path)) {
						const result = await wsRequest(socket, { t: "blob", path }, "blob", 60000);
						const bytes = Uint8Array.from(atob(result.data), (char) => char.charCodeAt(0));
						const blob = new Blob([bytes], { type: result.mime || "application/octet-stream" });
						if (blobUrlRef.current) URL.revokeObjectURL(blobUrlRef.current);
						blobUrlRef.current = URL.createObjectURL(blob);
						setOpenFile({ path, kind: isPdf(path) ? "pdf" : "image", url: blobUrlRef.current, version: result.version });
						setDirty(false);
						return;
					}
					const result = await wsRequest(socket, { t: "read", path, offset: 1, limit: 20000 }, "read", 60000);
					setOpenFile({ path, kind: "text", text: result.text, version: result.version });
					setDirty(false);
				} catch (failure) {
					const message = String(failure.message || failure);
					if (/二进制/.test(message)) {
						try {
							const result = await wsRequest(socket, { t: "blob", path }, "blob", 60000);
							const bytes = Uint8Array.from(atob(result.data), (char) => char.charCodeAt(0));
							const blob = new Blob([bytes], { type: result.mime || "application/octet-stream" });
							if (blobUrlRef.current) URL.revokeObjectURL(blobUrlRef.current);
							blobUrlRef.current = URL.createObjectURL(blob);
							setOpenFile({ path, kind: "pdf", url: blobUrlRef.current, version: result.version });
							setDirty(false);
						} catch (second) {
							setError(String(second.message || second));
						}
						return;
					}
					setError(message);
				}
			};

			/* CodeMirror 挂载 */
			React.useEffect(() => {
				if (!openFile || openFile.kind !== "text" || !editorHostRef.current) return;
				let disposed = false;
				loadCodemirror()
					.then(async (CodeMirror) => {
						const mode = modeOf(openFile.path);
						await loadMode(mode);
						if (disposed || !editorHostRef.current) return;
						if (editorRef.current) {
							editorRef.current.setValue(openFile.text);
							editorRef.current.setOption("mode", mode);
							return;
						}
						const editor = CodeMirror(editorHostRef.current, {
							value: openFile.text,
							mode,
							theme: "material-darker",
							lineNumbers: true,
							lineWrapping: false,
							styleActiveLine: true,
							indentUnit: 2,
							tabSize: 2,
							extraKeys: {
								"Cmd-S": () => save(),
								"Ctrl-S": () => save(),
							},
						});
						editor.on("change", () => setDirty(true));
						editorRef.current = editor;
						setTimeout(() => editor.refresh(), 30);
					})
					.catch((failure) => setError(String(failure.message || failure)));
				return () => {
					disposed = true;
				};
			}, [openFile]);

			const save = async () => {
				const socket = socketRef.current;
				if (!socket || socket.readyState !== 1 || !openFile || openFile.kind !== "text") return;
				const text = editorRef.current?.getValue() ?? openFile.text;
				try {
					const result = await wsRequest(socket, { t: "write", path: openFile.path, text }, "saved", 60000);
					setDirty(false);
					setSavedAt(Date.now());
					setOpenFile((previous) => (previous ? { ...previous, text, version: result.version } : previous));
				} catch (failure) {
					setError(String(failure.message || failure));
				}
			};

			const tree = (path, depth) => {
				const entries = listings[path];
				if (!entries) return null;
				return jsxRuntime.jsx("div", {
					children: entries.map((entry) => {
						const childPath = joinPath(path, entry.name);
						const isDir = entry.type === "directory";
						const isOpen = expanded.has(childPath);
						return jsxRuntime.jsxs(
							"div",
							{
								children: [
									jsxRuntime.jsxs(
										"div",
										{
											onClick: async () => {
												if (isDir) {
													setExpanded((previous) => {
														const next = new Set(previous);
														if (next.has(childPath)) next.delete(childPath);
														else next.add(childPath);
														return next;
													});
													if (!listings[childPath]) await loadDir(childPath);
													setDirPath(childPath);
												} else {
													setDirPath(path);
													await openPath(childPath);
												}
											},
											style: {
												padding: `2px 6px 2px ${8 + depth * 12}px`,
												cursor: "pointer",
												borderRadius: 3,
												fontSize: 12.5,
												whiteSpace: "nowrap",
												overflow: "hidden",
												textOverflow: "ellipsis",
												color: isDir ? "#4ec9b0" : "#d4d4d4",
												background: openFile?.path === childPath ? "#094771" : "transparent",
											},
											title: entry.name,
											children: [
												jsxRuntime.jsx("span", { children: isDir ? (isOpen ? "▾ " : "▸ ") : "  " }, "i"),
												jsxRuntime.jsx("span", { children: entry.name }, "n"),
											],
										},
									),
									isDir && isOpen ? tree(childPath, depth + 1) : null,
								],
							},
							childPath,
						);
					}),
				});
			};

			const header = jsxRuntime.jsxs("div", {
				style: { display: "flex", gap: 8, alignItems: "center", padding: "6px 8px", borderBottom: "1px solid #2d2d30", flexWrap: "wrap" },
				children: [
					jsxRuntime.jsx("select", {
						value: sel?.machine ?? "",
						onChange: (event) => switchSelection(event.target.value, ""),
						style: selectStyle,
						children: (catalog?.machines ?? []).map((machine) =>
							jsxRuntime.jsx("option", { value: machine.name, children: machine.label || machine.name }, machine.name),
						),
					}),
					jsxRuntime.jsx("select", {
						value: sel?.proj ?? "",
						onChange: (event) => switchSelection(sel?.machine ?? "", event.target.value),
						style: selectStyle,
						children: ((catalog?.machines ?? []).find((machine) => machine.name === sel?.machine)?.projs ?? []).map((proj) =>
							jsxRuntime.jsx("option", { value: proj.name, children: `${proj.name}` }, proj.name),
						),
					}),
					jsxRuntime.jsx("span", {
						style: { fontSize: 12, color: "#9d9d9d", fontFamily: "monospace", flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" },
						children: `${root}${dirPath ? `/${dirPath}` : ""}`,
					}),
					jsxRuntime.jsx("button", { onClick: () => loadDir(dirPath), style: buttonStyle, children: "⟳" }),
					jsxRuntime.jsx("span", {
						style: { fontSize: 11, color: "#6b6b6b" },
						children: `st=${status} · 根 ${listings[""]?.length ?? "-"} 项 · 已加载 ${Object.keys(listings).length} 目录`,
					}),
				],
			});

			const viewer =
				openFile?.kind === "pdf"
					? jsxRuntime.jsx("embed", { src: openFile.url, type: "application/pdf", style: { width: "100%", height: "100%", border: 0 } })
					: openFile?.kind === "image"
						? jsxRuntime.jsx("div", {
								style: { display: "grid", placeItems: "center", height: "100%", overflow: "auto" },
								children: jsxRuntime.jsx("img", { src: openFile.url, style: { maxWidth: "100%", maxHeight: "100%" } }),
							})
						: jsxRuntime.jsx("div", { ref: editorHostRef, style: { width: "100%", height: "100%", overflow: "hidden" } });

			const footer = jsxRuntime.jsxs("div", {
				style: { display: "flex", gap: 8, alignItems: "center", padding: "4px 8px", borderTop: "1px solid #2d2d30", fontSize: 12, color: "#9d9d9d" },
				children: [
					jsxRuntime.jsx("span", { style: { flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }, children: openFile?.path ?? "" }),
					dirty ? jsxRuntime.jsx("span", { style: { color: "#e5e510" }, children: "●" }) : null,
					openFile?.kind === "text"
						? jsxRuntime.jsx("button", { onClick: save, style: buttonStyle, children: "保存 ⌘S" })
						: null,
					savedAt > 0 && !dirty ? jsxRuntime.jsx("span", { style: { color: "#0dbc79" }, children: "已保存" }) : null,
				],
			});

			return jsxRuntime.jsxs("div", {
				style: { position: "relative", width: "100%", height: "100%", display: "flex", flexDirection: "column", background: "#1e1e1e", minHeight: 0 },
				children: [
					header,
					jsxRuntime.jsxs("div", {
						style: { flex: 1, display: "flex", minHeight: 0 },
						children: [
							jsxRuntime.jsx("div", {
								style: { width: 240, borderRight: "1px solid #2d2d30", overflow: "auto", padding: "4px 0" },
								children: status === "open" && sel ? tree("", 0) : jsxRuntime.jsx("div", { style: { padding: 12, color: "#9d9d9d", fontSize: 12 }, children: "连接中…" }),
							}),
							jsxRuntime.jsxs("div", {
								style: { flex: 1, minWidth: 0, display: "flex", flexDirection: "column" },
								children: [
									jsxRuntime.jsx("div", { style: { flex: 1, minHeight: 0 }, children: viewer }),
									footer,
								],
							}),
						],
					}),
					error
						? jsxRuntime.jsx("div", {
								style: { position: "absolute", left: 8, right: 8, bottom: 40, padding: "6px 10px", background: "#5a1d1d", color: "#f48771", fontSize: 12, borderRadius: 3 },
								children: error,
							})
						: null,
				],
			});
		}

		const buttonStyle = { background: "#0e639c", color: "#fff", border: 0, borderRadius: 3, padding: "2px 8px", cursor: "pointer", fontSize: 12 };
		const selectStyle = { background: "#3c3c3c", color: "#d4d4d4", border: "1px solid #4a4a4a", borderRadius: 3, fontSize: 12, padding: "1px 4px" };

		function WorkspaceGlyph() {
			/* 别用 ⌸：不少字体缺字形，iOS 上会渲染成「目」 */
			return jsxRuntime.jsx("span", { style: { fontFamily: "monospace", fontSize: 13 }, children: "▤" });
		}

		function workspaceDefinition(t) {
			return {
				id: WORKSPACE_ID,
				kind: WORKSPACE_ID,
				priority: "builtin",
				title: () => t("type.label"),
				guide: [{ order: 25, title: () => t("guide.title"), description: () => t("guide.description"), icon: WorkspaceGlyph }],
			};
		}


		/**
		 * 手机端抽屉：窄屏且侧栏被展开时给 body 加 lab-drawer-open，
		 * 让 CSS 把它变成浮层；点遮罩自动收起。
		 * DSH 的类名带 hash，这里只依赖语义后缀 sidebarCol，用宽度判定展开状态。
		 */
		function setupMobileDrawer() {
			const query = window.matchMedia("(max-width: 640px)");
			const sidebarElement = () => document.querySelector('[class*="sidebarCol"]');

			/*
			 * 不能用「侧栏宽度」判断：CSS 把宽度强制成 330px 之后会永远成立（自反馈）。
			 * 用 DSH 自己的按钮标签：展开时是「收起侧边栏」，收起时是「打开侧边栏」。
			 */
			const isExpanded = () =>
				[...document.querySelectorAll("button")].some((button) =>
					/收起侧边栏|Collapse sidebar/i.test(button.getAttribute("aria-label") || ""),
				);

			const update = () => {
				if (!query.matches) {
					document.body.classList.remove("lab-drawer-open");
					return;
				}
				document.body.classList.toggle("lab-drawer-open", Boolean(sidebarElement()) && isExpanded());
			};

			/* 点抽屉外面就收起 */
			document.addEventListener(
				"click",
				(event) => {
					if (!document.body.classList.contains("lab-drawer-open")) return;
					const element = sidebarElement();
					if (element && element.contains(event.target)) return;
					const collapse = [...document.querySelectorAll("button")].find((button) =>
						/收起侧边栏/.test(button.getAttribute("aria-label") || ""),
					);
					collapse?.click();
				},
				true,
			);

			new MutationObserver(update).observe(document.body, {
				subtree: true,
				childList: true,
				attributes: true,
				attributeFilter: ["class", "style"],
			});
			setInterval(update, 500);
			query.addEventListener("change", update);
			update();
		}


		/* ================================================================
		 * 进度计时：长步骤期间界面只有「深度求索中…」，断线重连后尤其像卡死。
		 * 这里在状态文字后面挂一个计时器，明确告诉人「还在跑、已经等了多久」。
		 * 用 DOM 观察而不是内部状态，是为了不依赖 DSH 私有 API；找不到状态就不显示。
		 * ================================================================ */

		const PROGRESS_STATUS = /^(深度求索中|思考中|执行中|等待批准|工作中|Running|Thinking)[…\.\!\s]*$/;

		function installProgressChip() {
			if (window.__labProgressInstalled) return;
			window.__labProgressInstalled = true;

			let startedAt = 0;
			let chip = null;
			let timer = null;

			/** 状态文字（深度求索中…）。 */
			const findStatus = () => {
				const nodes = document.querySelectorAll("span, div");
				for (const node of nodes) {
					if (node.children.length > 0) continue;
					const text = (node.textContent || "").trim();
					if (text.length > 0 && text.length <= 20 && PROGRESS_STATUS.test(text)) return node;
				}
				return null;
			};

			/** 回合进行中的「停止」按钮 —— 工具执行阶段没有状态文字，靠它判断还在跑。 */
			const findStopButton = () =>
				[...document.querySelectorAll("button")].find((button) => /停止|Stop/i.test(button.getAttribute("aria-label") || "")) ?? null;

			/** 返回 {anchor, before}：把计时器插到哪。 */
			const findAnchor = () => {
				const status = findStatus();
				if (status?.parentElement) return { anchor: status, before: false };
				const stop = findStopButton();
				if (stop?.parentElement) return { anchor: stop, before: true };
				return null;
			};

			const tick = () => {
				if (!chip) return;
				const seconds = Math.floor((Date.now() - startedAt) / 1000);
				chip.textContent =
					seconds < 60
						? `· ${seconds}s`
						: `· 已等待 ${Math.floor(seconds / 60)}m${String(seconds % 60).padStart(2, "0")}s（离开页面也不会中断）`;
			};

			const detach = () => {
				startedAt = 0;
				if (timer) clearInterval(timer);
				timer = null;
				chip?.remove();
				chip = null;
			};

			const attach = () => {
				const target = findAnchor();
				if (!target) {
					detach();
					return;
				}
				const place = () => {
					if (!chip) return;
					if (target.before) target.anchor.parentElement?.insertBefore(chip, target.anchor);
					else target.anchor.parentElement?.insertBefore(chip, target.anchor.nextSibling);
				};
				if (!startedAt) {
					startedAt = Date.now();
					chip = document.createElement("span");
					chip.id = "lab-progress";
					chip.style.cssText = "margin-left:6px;font-size:12px;opacity:.7;font-variant-numeric:tabular-nums";
					place();
					timer = setInterval(tick, 1000);
					tick();
					return;
				}
				/* 节点被重渲染掉了（或从「思考中」换到「工具执行中」）就重新挂 */
				if (chip && !chip.isConnected) place();
			};

			let queued = false;
			const schedule = () => {
				if (queued) return;
				queued = true;
				queueMicrotask(() => {
					queued = false;
					try {
						attach();
					} catch {
						/* 布局变化期间忽略 */
					}
				});
			};

			new MutationObserver(schedule).observe(document.body, {
				subtree: true,
				childList: true,
				characterData: true,
			});
			schedule();
		}

		/* ================================================================
		 * 手机端补充样式（只在窄屏生效，桌面端零影响）
		 * ================================================================ */

		const MOBILE_CSS = `
@media (max-width: 640px) {
	/* 去掉 iOS 点击时的灰色高亮块 */
	* { -webkit-tap-highlight-color: transparent; }

	/* 图标条按钮：36px → 40px，手指更好点 */
	[class*="sidebarCol"] button {
		min-width: 40px;
		min-height: 40px;
	}

	/* 底部安全区：iPhone 的 home indicator 不要压住输入框 */
	body {
		padding-bottom: env(safe-area-inset-bottom);
		overscroll-behavior-y: contain;
	}

	/* 输入框 16px 以上，iOS 才不会在聚焦时自动放大页面 */
	textarea, input[type="text"], [contenteditable="true"] {
		font-size: max(16px, 1em);
	}

	/*
	 * 侧栏展开时改成抽屉。DSH 默认把侧栏当 flex 兄弟，展开后聊天区只剩
	 * ~110px；这里让它脱离文档流浮在上面，主内容保持满宽。
	 */
	body.lab-drawer-open [class*="sidebarCol"] {
		position: fixed;
		top: 0;
		bottom: 0;
		left: 0;
		z-index: 60;
		width: min(86vw, 330px) !important;
		padding-top: env(safe-area-inset-top);
		box-shadow: 0 0 32px rgba(0, 0, 0, 0.45);
	}
	/* 抽屉背后的遮罩：点它收起 */
	body.lab-drawer-open::before {
		content: "";
		position: fixed;
		inset: 0;
		z-index: 55;
		background: rgba(0, 0, 0, 0.35);
	}
}
`;

		/** 只注入一次。 */
		function ensureMobileStyles() {
			if (document.getElementById("lab-mobile-css")) return;
			const style = document.createElement("style");
			style.id = "lab-mobile-css";
			style.textContent = MOBILE_CSS;
			document.head.appendChild(style);
		}

		/* ── 品牌：把 deepseek 换成 macchiato harness ─────────────────────────── */
		const BRAND_CSS = `
[data-slot="sidebar.brand.mark"] > svg:not(.macchiato-mark),
[data-slot="conversation.hero.brand.mark"] > svg:not(.macchiato-mark) { display: none !important; }
[data-slot="sidebar.brand.name"] > svg { display: none !important; }
.macchiato-brand { display: inline-flex; align-items: center; gap: 8px; color: inherit; }
.macchiato-word {
	font-size: 17px; font-weight: 600; letter-spacing: -0.02em; line-height: 1;
	text-transform: lowercase;
}
.macchiato-badge {
	font-size: 10px; font-weight: 700; letter-spacing: 0.08em; line-height: 1;
	padding: 3px 6px 2px; border-radius: 5px;
	border: 1.5px solid currentColor; opacity: 0.9;
}
`;

		function ensureBrandStyles() {
			if (document.getElementById("lab-brand-css")) return;
			const style = document.createElement("style");
			style.id = "lab-brand-css";
			style.textContent = BRAND_CSS;
			document.head.appendChild(style);
		}

		/** 一杯 macchiato（白色描边，跟随主题色）。 */
		function MacchiatoMark(props) {
			const size = props && props.size ? props.size : 24;
			return jsxRuntime.jsxs("svg", {
				width: size,
				height: size,
				viewBox: "0 0 24 24",
				fill: "none",
				"aria-hidden": true,
				className: "macchiato-mark",
				children: [
					jsxRuntime.jsx("path", {
						d: "M4 10.2h11.4v4.7a5.2 5.2 0 0 1-5.2 5.2H9.2A5.2 5.2 0 0 1 4 14.9v-4.7Z",
						stroke: "currentColor",
						strokeWidth: 1.6,
						strokeLinejoin: "round",
					}),
					jsxRuntime.jsx("path", {
						d: "M15.4 11.2h1.7a2.6 2.6 0 0 1 0 5.2h-1.7",
						stroke: "currentColor",
						strokeWidth: 1.6,
						strokeLinecap: "round",
					}),
					jsxRuntime.jsx("path", {
						d: "M8 6.7c0-1.1 1-1.3 1-2.4M12 6.7c0-1.1 1-1.3 1-2.4",
						stroke: "currentColor",
						strokeWidth: 1.4,
						strokeLinecap: "round",
						opacity: 0.7,
					}),
				],
			});
		}

		function MacchiatoName() {
			return jsxRuntime.jsxs("span", {
				className: "macchiato-brand",
				children: [
					jsxRuntime.jsx("span", { className: "macchiato-word", children: "macchiato" }),
					jsxRuntime.jsx("span", { className: "macchiato-badge", children: "HARNESS" }),
				],
			});
		}

		/* 图标（咖啡杯），同时用于 favicon；内嵌 data URI 免得依赖静态资源 */
		const MACCHIATO_FAVICON =
			"data:image/svg+xml," +
			encodeURIComponent(
				'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">' +
					'<rect width="32" height="32" rx="7" fill="#111214"/>' +
					'<g stroke="#ffffff" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round">' +
					'<path d="M8 13h13v6.2a5.6 5.6 0 0 1-5.6 5.6h-1.8A5.6 5.6 0 0 1 8 19.2V13Z"/>' +
					'<path d="M21 14.3h1.7a2.8 2.8 0 0 1 0 5.6H21"/>' +
					'<path d="M12.4 9.4c0-1.2 1.1-1.4 1.1-2.6M16.4 9.4c0-1.2 1.1-1.4 1.1-2.6" opacity=".7"/>' +
					"</g></svg>",
			);

		function applyBrand(ctx) {
			ensureBrandStyles();
			const title = "macchiato harness";
			const previousTitle = document.title;

			/*
			 * 应用自己会在路由变化时重设 document.title / favicon，这里钉住：
			 * 用 MutationObserver 监听 <head>，一旦被改回去就立刻改回来（防抖到下一帧）。
			 */
			const pinHead = () => {
				if (document.title !== title) document.title = title;
				const icon = document.querySelector('link[rel~="icon"]');
				if (icon && icon.getAttribute("href") !== MACCHIATO_FAVICON) {
					icon.setAttribute("href", MACCHIATO_FAVICON);
				}
			};
			pinHead();
			let queued = false;
			const observer = new MutationObserver(() => {
				if (queued) return;
				queued = true;
				requestAnimationFrame(() => {
					queued = false;
					pinHead();
				});
			});
			observer.observe(document.head, { childList: true, subtree: true, characterData: true });
			observer.observe(document.documentElement, { childList: true, subtree: false });

			const link = document.querySelector('link[rel~="icon"]');
			const previousHref = link ? link.getAttribute("href") : null;
			if (link) link.setAttribute("href", MACCHIATO_FAVICON);

			const effect = ctx.effect(
				() =>
					ctx.slots.inject("sidebar.brand.mark", () =>
						ctx.slots.register({ name: "sidebar.brand.mark", key: "macchiato" }, MacchiatoMark),
					),
				"brand: sidebar mark",
			);
			ctx.effect(
				() =>
					ctx.slots.inject("sidebar.brand.name", () =>
						ctx.slots.register({ name: "sidebar.brand.name", key: "macchiato" }, MacchiatoName),
					),
				"brand: sidebar name",
			);
			ctx.effect(
				() =>
					ctx.slots.inject("conversation.hero.brand.mark", () =>
						ctx.slots.register({ name: "conversation.hero.brand.mark", key: "macchiato" }, () =>
							jsxRuntime.jsx(MacchiatoMark, { size: 34 }),
						),
					),
				"brand: hero mark",
			);
			return () => {
				observer.disconnect();
				document.title = previousTitle;
				if (link && previousHref !== null) link.setAttribute("href", previousHref);
				if (typeof effect === "function") effect();
			};
		}

		const inject = ["slots", "locale", "sidebarRightTabs"];

		function apply(ctx) {
			const t = ctx.locale.bind(NS);
			ctx.effect(() => ctx.sidebarRightTabs.register(terminalDefinition(t)), "lab-terminal: tab type");
			ensureMobileStyles();
			applyBrand(ctx);
			setupMobileDrawer();
			installProgressChip();
			const tw = ctx.locale.bind("labWorkspace");
			ctx.effect(() => ctx.sidebarRightTabs.register(workspaceDefinition(tw)), "lab-workspace: tab type");
			ctx.effect(() => ctx.locale.register("labWorkspace", { zh: wsText, en: wsEnglish }), "lab-workspace: dictionaries");
			ctx.effect(
				() =>
					ctx.slots.inject("sidebar.right.pane.tab", () =>
						ctx.slots.register({ name: "sidebar.right.pane.tab", key: WORKSPACE_ID, locale: "labWorkspace" }, WorkspaceBody),
					),
				"lab-workspace: tab body",
			);
			ctx.effect(
				() =>
					ctx.slots.inject("sidebar.right.pane.tab.title", () =>
						ctx.slots.register({ name: "sidebar.right.pane.tab.title", key: WORKSPACE_ID }, () =>
							jsxRuntime.jsx("span", { children: "工作区" }),
						),
					),
				"lab-workspace: tab title",
			);
			ctx.effect(() => ctx.locale.register(NS, { zh, en }), "lab-terminal: dictionaries");
			ctx.effect(
				() =>
					ctx.slots.inject("sidebar.right.pane.tab", () =>
						ctx.slots.register({ name: "sidebar.right.pane.tab", key: TERMINAL_ID, locale: NS }, TerminalBody),
					),
				"lab-terminal: tab body",
			);
			ctx.effect(
				() =>
					ctx.slots.inject("sidebar.right.pane.tab.title", () =>
						ctx.slots.register({ name: "sidebar.right.pane.tab.title", key: TERMINAL_ID }, TerminalTitle),
					),
				"lab-terminal: tab title",
			);
		}

		exports.apply = apply;
		exports.inject = inject;
		return module.exports;
	},
});
