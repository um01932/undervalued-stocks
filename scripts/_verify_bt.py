import re
content = open('docs/index.html', encoding='utf-8').read()
print('btSwitchTab in HTML:', 'btSwitchTab' in content)
panels = re.findall(r'bt-panel-([A-Z0-9]+)', content)
print('bt-tab panels found:', sorted(set(panels)))
btns = re.findall(r'data-tab="([A-Z0-9]+)"', content)
print('bt-tab buttons found:', sorted(set(btns)))
idx = content.find('bt-panel')
if idx >= 0:
    segment = content[idx:idx+60000]
    print('Has 2021 in BT section:', '2021' in segment)
    print('Has 2022 in BT section:', '2022' in segment)
    print('Has 2025 in BT section:', '2025' in segment)
print('Report size:', len(content)//1024, 'KB')
