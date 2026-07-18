# 部署产物

## msgflow-listing.service
listing_alert watcher 的 systemd --user service（常驻，5s 轮询上币公告 → Lark）。

安装：
```bash
cp deploy/msgflow-listing.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now msgflow-listing.service
loginctl enable-linger $USER   # 确保登出/重启后仍运行
```

其余采集器走 cron：`crontab scripts/crontab.example`
