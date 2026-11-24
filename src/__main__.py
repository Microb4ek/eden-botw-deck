"""Offline BOTW 1.6.0 setup for Eden on SteamOS."""
import argparse
import base64
import configparser
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

TITLE = '01007EF00011E000'
BUILD = '8E9978D50BDD20B4C8395A106C27FFDE'
HOME = Path.home()
STATE = Path(os.getenv('XDG_STATE_HOME') or HOME/'.local/state')/'eden-botw-setup'
TITLEBAR = 'Zelda · Eden · Steam Deck'

def ask(text, items):
    if shutil.which('zenity') and (os.getenv('DISPLAY') or os.getenv('WAYLAND_DISPLAY')):
        result = subprocess.run(['zenity','--list','--title',TITLEBAR,'--width=680','--height=340',
                                 '--text',text,'--column','Выбор',*items],capture_output=True,text=True)
        if result.returncode: raise SystemExit(0)
        return result.stdout.strip()
    print(text)
    for i, item in enumerate(items,1): print(i,item)
    index = int(input('Номер: '))-1
    if not 0 <= index < len(items): raise ValueError('Неверный номер')
    return items[index]

def ini(content):
    cfg = configparser.RawConfigParser(strict=False,delimiters=('=',),allow_no_value=True)
    cfg.optionxform = str
    cfg.read_string(content.lstrip('\ufeff'))
    return cfg

def merged(text, section, changes):
    # Edit individual keys without reserializing unrelated Qt/SDL settings.
    lines = text.splitlines(keepends=True)
    indexes = [i for i,line in enumerate(lines) if line.strip().lstrip('\ufeff') == '['+section+']']
    if len(indexes)>1: raise ValueError('Повторяющийся раздел '+section)
    if not indexes:
        lines.extend(['\n','['+section+']\n'])
        begin = len(lines)
    else: begin = indexes[0]+1
    end = next((i for i in range(begin,len(lines)) if lines[i].lstrip().startswith('[')), len(lines))
    block = []
    missing = dict(changes)
    for line in lines[begin:end]:
        k, sep, value = line.partition('=')
        k=k.strip()
        if sep and k in changes:
            if k in missing and changes[k] is not None:
                block.append(line if value.strip()==changes[k] else k+'='+changes[k]+'\n')
            missing.pop(k,None)
        else: block.append(line)
    if block and not block[-1].endswith('\n'): block[-1]+='\n'
    block.extend(k+'='+v+'\n' for k,v in missing.items() if v is not None)
    if begin and not lines[begin-1].endswith('\n'): lines[begin-1]+='\n'
    return ''.join(lines[:begin]+block+lines[end:])

def locate(manual):
    config = Path(os.getenv('XDG_CONFIG_HOME') or HOME/'.config')
    data = Path(os.getenv('XDG_DATA_HOME') or HOME/'.local/share')
    pairs = [(config/'eden', data/'eden')]
    for root in (HOME/'.var/app').glob('*'):
        pairs.append((root/'config/eden',root/'data/eden'))
    for base in [Path.cwd(),HOME/'Applications',HOME/'Desktop',HOME/'Downloads']:
        pairs.extend((p/'config',p) for p in [base/'user',*base.glob('*.AppImage.home/user')])
    if manual:
        cfg=manual.resolve()
    else:
        matches=list(dict.fromkeys(str(c/'qt-config.ini') for c,d in pairs if (c/'qt-config.ini').is_file()))
        if not matches:
            if shutil.which('zenity') and os.getenv('DISPLAY'):
                r=subprocess.run(['zenity','--file-selection','--title=Выбери qt-config.ini своего Eden'],capture_output=True,text=True)
                if r.returncode: raise SystemExit(0)
                cfg=Path(r.stdout.strip())
            else: raise ValueError('Открой Eden один раз или укажи --config /путь/к/qt-config.ini')
        else: cfg=Path(matches[0] if len(matches)==1 else ask('Выбери свою установку Eden',matches))
    if cfg.name!='qt-config.ini' or not cfg.is_file(): raise ValueError('Нужен существующий qt-config.ini Eden')
    doc=ini(cfg.read_text())
    load=next((doc.get(sec,'load_directory',fallback='').strip('"') for sec in ('Data%20Storage','Data Storage') if doc.get(sec,'load_directory',fallback='').strip('"')), '')
    if load and not Path(load).is_absolute(): raise ValueError('Неизвестный формат load_directory: '+load)
    fallback=next((d for c,d in pairs if c.resolve()==cfg.parent.resolve()),cfg.parent.parent if cfg.parent.name=='config' else None)
    if not load and fallback is None: raise ValueError('Укажи каталог модов Eden в Data Storage и повтори запуск')
    return cfg, (Path(load) if load else fallback/'load')/TITLE

def atomic(path, data):
    if data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        if path.exists(): shutil.copymode(path,tmp)
        os.replace(tmp,path)
    finally: Path(tmp).unlink(missing_ok=True)

def get(path):
    return path.read_bytes() if path.exists() else None

def enc(data):
    return None if data is None else base64.b64encode(data).decode()

def dec(data):
    return None if data is None else base64.b64decode(data)

def undo(journal):
    records=json.loads(journal.read_text())
    for name,item in reversed(list(records.items())):
        path=Path(name)
        old,new=dec(item['old']),dec(item['new'])
        current=get(path)
        if current==old: continue
        if current!=new:
            # Preserve edits made after setup; revert only our profile keys.
            if 'keys' in item and current is not None:
                text=current.decode()
                original=ini((old or b'').decode())
                installed=ini((new or b'').decode())
                now=ini(text)
                for sec,keys in item['keys'].items():
                    delta={k:original.get(sec,k,fallback=None) for k in keys if now.get(sec,k,fallback=None)==installed.get(sec,k,fallback=None)}
                    if delta: text=merged(text,sec,delta)
                atomic(path,text.encode())
            # A changed third-party patch belongs to the user.
            continue
        atomic(path,old)
    os.replace(journal, journal.with_suffix(".previous.json"))

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=['full','balanced','restore'])
    parser.add_argument('--config',type=Path)
    args=parser.parse_args()
    labels={'1080p · цель 60 FPS':'full','Штатное разрешение Docked · цель 60 FPS':'balanced','Вернуть прежние настройки':'restore'}
    mode=args.mode or labels[ask('BOTW для Nintendo Switch · обновление 1.6.0',list(labels))]
    for comm in Path('/proc').glob('[0-9]*/comm'):
        try: proc=comm.read_text().strip().lower()
        except OSError: continue
        if proc in ('eden','eden-qt','eden-room','eden-cli') or proc.startswith('eden-linux'):
            raise ValueError('Закрой Eden обычным способом и снова запусти установщик.')
    cfg,mods=locate(args.config)
    STATE.mkdir(parents=True,exist_ok=True)
    lock=(STATE/'lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    journal=STATE/(hashlib.sha256(str(cfg.resolve()).encode()).hexdigest()[:20]+'.json')
    if journal.exists(): undo(journal)
    if mode=='restore': return 'Прежние настройки восстановлены.'
    profile=cfg.parent/'custom'/(TITLE+'.ini')
    plans={}
    def plan(path,data,keys=None):
        old=get(path)
        if old!=data:
            plans[str(path)]={'old':enc(old),'new':enc(data)}
            if keys: plans[str(path)]['keys']=keys
    global_cfg=ini(cfg.read_text())
    disabled=set()
    for k,v in global_cfg.items('DisabledAddOns') if global_cfg.has_section('DisabledAddOns') else []:
        if k.endswith('\\title_id') and v.strip('"').upper() in (TITLE,str(int(TITLE,16)),'0X'+TITLE):
            prefix=k.split('\\')[0]+'\\disabled\\'
            disabled.update(val.strip('"') for key,val in global_cfg.items('DisabledAddOns') if key.startswith(prefix) and key.endswith('\\d'))
    if 'Update' in disabled: raise ValueError('Включи обновление 1.6.0 в Eden → Add-Ons.')
    with zipfile.ZipFile(sys.argv[0]) as z:
        base=ini(z.read(TITLE+'.ini').decode())
        text=(get(profile) or b'').decode()
        for sec in base.sections(): text=merged(text,sec,dict(base.items(sec)))
        plan(profile,text.encode(),{sec:list(base[sec]) for sec in base.sections()})
        for name in z.namelist():
            if not name.endswith('.pchtxt'): continue
            mod,_,file=name.split('/')
            folder=mod
            while folder in disabled: folder+=' (Deck)'
            path=mods/folder/'exefs'/file
            patch=z.read(name)
            if get(path) not in (None,patch):
                raise ValueError('Другой мод уже использует файл '+str(path))
            plan(path,None if mode=='balanced' and mod=='Deck Docked 1080p' else patch)
        # Identify overlapping text patches for this exact executable before switching on the pack.
        addresses = set()
        for n in z.namelist():
            if n.endswith(".pchtxt"):
                addresses.update(re.findall(rb"(?m)^([0-9A-Fa-f]{8})[ \t]+[0-9A-Fa-f]{8}",z.read(n).upper()))
        for other in mods.glob("*/exefs/*.pchtxt"):
            folder = other.parent.parent.name
            if folder in disabled or str(other) in plans: continue
            if other.relative_to(mods).as_posix() in z.namelist(): continue
            if folder.endswith(" (Deck)") and any(get(other)==z.read(n) for n in z.namelist() if n.endswith(".pchtxt")): continue
            raw=other.read_bytes().upper()
            if BUILD.encode() in raw and addresses.intersection(re.findall(rb"(?m)^([0-9A-F]{8})[ \t]+",raw)):
                # Back up and suspend only an exact duplicate of a supplied patch.
                normalized = lambda b: b.replace(b"\r\n",b"\n").strip()
                if any(normalized(raw)==normalized(z.read(n).upper()) for n in z.namelist() if n.endswith(".pchtxt")):
                    plan(other,None)
                else:
                    raise ValueError("В Add-Ons отключи мод с пересекающимся патчем: "+folder)
    if plans:
        atomic(journal,json.dumps(plans).encode())
        try:
            for name,r in plans.items(): atomic(Path(name),dec(r['new']))
        except BaseException:
            undo(journal)
            raise
    return 'Готово. Профиль: '+str(profile)+'\nМоды: '+str(mods)+'\nЦель — 60 FPS. Реальная частота зависит от сцены.'

if __name__=='__main__':
    try:
        message=main()
    except Exception as e:
        print('Ошибка:',str(e) or type(e).__name__, file=sys.stderr)
        sys.exit(1)
    print(message)
