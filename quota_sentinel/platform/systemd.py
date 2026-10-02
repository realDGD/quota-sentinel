"""Literal systemd user unit arguments; no shell or environment expansion."""

def quoted(value):
 return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'

def render(d):
 lines=['[Unit]','Description=Quota Sentinel selected components','[Service]','Type=simple','ExecStart=:'+ ' '.join(quoted(x) for x in d.argv),'WorkingDirectory='+quoted(d.cwd),'UMask=0077','Restart=on-failure','RestartSec=30','KillMode=control-group','TimeoutStopSec='+str(d.stop_timeout),'SendSIGKILL=yes']
 lines.extend('Environment='+quoted(k+'='+v) for k,v in sorted(d.environment.items()))
 return ('\n'.join(lines+['[Install]','WantedBy=default.target',''])).encode()
