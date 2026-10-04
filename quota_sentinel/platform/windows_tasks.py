"""Task Scheduler XML: this user, interactive token, no stored password."""
import subprocess
import xml.etree.ElementTree as ET
NS='http://schemas.microsoft.com/windows/2004/02/mit/task'

def render(d,user_id,manifest):
 ET.register_namespace('',NS)
 def add(parent,tag,text=None,**attrs):
  node=ET.SubElement(parent,'{'+NS+'}'+tag,attrs)
  if text is not None:node.text=str(text)
  return node
 root=ET.Element('{'+NS+'}Task',version='1.3')
 info=add(root,'RegistrationInfo');add(info,'Author',user_id);add(info,'URI','\\'+d.name)
 triggers=add(root,'Triggers');logon=add(triggers,'LogonTrigger');add(logon,'Enabled','true');add(logon,'UserId',user_id)
 principals=add(root,'Principals');principal=add(principals,'Principal',id='User');add(principal,'UserId',user_id);add(principal,'LogonType','InteractiveToken');add(principal,'RunLevel','LeastPrivilege')
 settings=add(root,'Settings')
 for tag,value in [('MultipleInstancesPolicy','IgnoreNew'),('DisallowStartIfOnBatteries','false'),('StopIfGoingOnBatteries','false'),('AllowHardTerminate','true'),('StartWhenAvailable','true'),('Enabled','true'),('ExecutionTimeLimit','PT0S')]:add(settings,tag,value)
 restart=add(settings,'RestartOnFailure');add(restart,'Interval','PT1M');add(restart,'Count',3)
 actions=add(root,'Actions',Context='User');execute=add(actions,'Exec');add(execute,'Command',d.argv[0]);add(execute,'Arguments',subprocess.list2cmdline(('-m','quota_sentinel.platform.service_host',str(manifest))));add(execute,'WorkingDirectory',d.cwd)
 # schtasks imports a Unicode XML file; its UTF-8-without-BOM path may be
 # decoded as ANSI before the declaration and fail to switch encodings.
 return ET.tostring(root,encoding='utf-16',xml_declaration=True)
