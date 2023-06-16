## Template systemd unit file
```
[Unit]
Description=/kbot service
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
Restart=always
RestartSec=1
StartLimitBurst=5
StartLimitIntervalSec=10
User=pi
ExecStart=/usr/bin/python main.py
WorkingDirectory=/home/pi/repos/kbot

[Install]
WantedBy=multi-user.target
```