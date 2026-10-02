"""One LaunchAgent for the selected shared host."""
import math
import plistlib

def render(d):
 return plistlib.dumps({'Label':d.name,'ProgramArguments':list(d.argv),'WorkingDirectory':str(d.cwd),'EnvironmentVariables':dict(d.environment),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':30,'ExitTimeOut':math.ceil(d.stop_timeout),'Umask':63,'StandardOutPath':str(d.cwd/'logs/service.out.log'),'StandardErrorPath':str(d.cwd/'logs/service.err.log')})
