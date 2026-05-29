# Nginx 反代（与 `/remote/` 同站 :80）

Dashboard 走两个路径（与现有 `sports` 站点合并）：

| 公网路径 | 作用 |
|---------|------|
| `/login` | 登录页 |
| `/console/` | Web 控制台（Chat / Settings / Kernel） |

后端：`macchiato-dashboard` 监听 `127.0.0.1:18765`。

## 1. systemd

```bash
sudo ./deploy/systemd/install.sh "$(pwd)" ubuntu --with-dashboard
sudo systemctl enable --now macchiato-dashboard.service
curl -sS http://127.0.0.1:18765/login
curl -sS http://127.0.0.1:18765/console/api/health
```

## 2. 合并进现有 Nginx（推荐）

把 `deploy/nginx/sports-locations.snippet` 的内容粘贴到 `/etc/nginx/sites-available/sports`，**放在博客 catch-all `location /` 之前**（与 `/remote/` 同级）。

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## 3. 访问

- 登录：`http://你的IP/login`
- 控制台：`http://你的IP/console/`

`dashboard_auth.yaml` 使用 HTTP 时：`secure_cookies: false`。

## 4. 清理独立站点（若之前装过）

```bash
sudo rm -f /etc/nginx/sites-enabled/macchiato-dashboard
sudo nginx -t && sudo systemctl reload nginx
```

## 5. 与 remote 并存

```
/remote/   -> 127.0.0.1:9380
/login     -> 127.0.0.1:18765
/console/  -> 127.0.0.1:18765
/          -> 127.0.0.1:8765  (blog)
```

独立 HTTPS 子域名模板仍见 `macchiato-dashboard.conf.in`（有证书时用）。
