"""QNX 8.0 BSP/SLM host compiler and deterministic output replay; no target execution."""
import argparse
import heapq
import json
import math
from pathlib import Path
import re
import shlex
import xml.etree.ElementTree as ET
from xml.parsers import expat

TOKEN = re.compile(r'[A-Za-z0-9_./,:+^=-]+')
PATH = re.compile(r'/[A-Za-z0-9_./-]+')


def positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError('Positive finite ' + name + ' required')


def validate(config):
    if config.get('qnx_version') != '8.0' or config.get('parallel_startup_permitted') is not True:
        raise ValueError('Explicit QNX 8.0 and platform parallel permission required')
    runtime = config['runtime']
    if runtime.get('slogger2_already_running') is not True:
        raise ValueError('SLM requires slogger2 already running in the reviewed BSP')
    for name in ('slm', 'sh', 'waitfor', 'ready_script', 'slm_config'):
        if not PATH.fullmatch(runtime[name]): raise ValueError('Absolute runtime path required: ' + name)
    services = config['services']
    if not 1 <= len(services) <= 100: raise ValueError('Provide 1 to 100 services')
    by_id = {s['id']: s for s in services}
    if len(by_id) != len(services): raise ValueError('Duplicate service ID')
    for s in services:
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', s['id']) or s['id'].startswith('bw_'):
            raise ValueError('Invalid or reserved service ID')
        if any(k in s for k in ('command', 'env', 'environment', 'modifiers')):
            raise ValueError('Raw command, environment and buildfile modifiers unsupported; use executable/args')
        if not PATH.fullmatch(s['executable']) or not isinstance(s['args'], list) or any(not isinstance(a, str) or not TOKEN.fullmatch(a) for a in s['args']):
            raise ValueError('One absolute executable and plain separate argument tokens required')
        if s.get('parallel_safe') is not True: raise ValueError('Explicit service parallel permission required')
        positive(s['ready_after_s'], 'ready_after_s'); positive(s['timeout_s'], 'timeout_s')
        ret = s['launch_return_s']
        if isinstance(ret, bool) or not isinstance(ret, (int, float)) or not math.isfinite(ret) or ret < 0:
            raise ValueError('Nonnegative finite launch_return_s required')
        deps = s['requires']
        if len(set(deps)) != len(deps) or any(d not in by_id or d == s['id'] for d in deps):
            raise ValueError('Unknown, repeated or self dependency')
        if s['phase'] == 'bsp':
            if deps: raise ValueError('BSP subset supports independent launches only; put gated work under SLM')
            if s['launch_mode'] not in ('background', 'self_daemon'): raise ValueError('BSP launch mode required')
        elif s['phase'] == 'slm':
            if s['launch_mode'] != 'foreground': raise ValueError('SLM-managed processes must stay foreground')
        else: raise ValueError('phase must be bsp or slm')
        if Path(s['executable']).name == 'screen' and (s['phase'] != 'bsp' or s['launch_mode'] != 'self_daemon'):
            raise ValueError('Supported Screen route is BSP self_daemon, without ampersand')
        ready = s['readiness']
        if ready['kind'] == 'path':
            if not PATH.fullmatch(ready['value']): raise ValueError('Absolute readiness path required')
            if s.get('network_interface'): raise ValueError('A network interface is not a device pathname')
        elif ready['kind'] == 'exits':
            if s['phase'] != 'slm' or ready.get('value') != 0: raise ValueError('Only SLM expected exit 0 supported')
        else: raise ValueError('Use path or explicit exit-0 component; interface shorthand unsupported')
    pending = set(by_id); done = set()
    while pending:
        available = {n for n in pending if set(by_id[n]['requires']) <= done}
        if not available: raise ValueError('Dependency cycle')
        done |= available; pending -= available
    if config['first_useful_application'] not in by_id: raise ValueError('Unknown first useful application')
    return by_id


def ready_id(s):
    return 'bw_ready_' + s['id'] if s['readiness']['kind'] == 'path' else s['id']


def ready_helper(config):
    """External shell script, never an if statement inside a QNX buildfile."""
    return ('#!/bin/sh\n# Installed at ready_script; invoked through the declared shell.\n'
            'if [ "$#" -ne 2 ]; then exit 64; fi\n'
            + config['runtime']['waitfor'] + ' "$1" "$2" || exit 1\n'
            '[ -e "$1" ] || exit 1\nexit 0\n')


def slm_xml(config):
    services = validate(config); root = ET.Element('SLM:system')
    def tag(parent, name, value, **attrs):
        e = ET.SubElement(parent, 'SLM:' + name, attrs); e.text = str(value); return e
    for s in services.values():
        if s['phase'] == 'slm':
            c = ET.SubElement(root, 'SLM:component', {'name': s['id']})
            for d in s['requires']: tag(c, 'depend', ready_id(services[d]), state='session')
            tag(c, 'command', s['executable']); tag(c, 'args', ' '.join(s['args'])); tag(c, 'repair', 'stop')
            if s['readiness']['kind'] == 'exits':
                tag(c, 'waitfor', 0, wait='exits', polltime='100:' + str(math.ceil(s['timeout_s']*1000)))
            else: tag(c, 'waitfor', '', wait='none')
        if s['readiness']['kind'] == 'path':
            c = ET.SubElement(root, 'SLM:component', {'name': ready_id(s)})
            if s['phase'] == 'slm': tag(c, 'depend', s['id'], state='session')
            tag(c, 'command', config['runtime']['sh'])
            timeout = math.ceil(s['timeout_s']*10)/10
            tag(c, 'args', f"{config['runtime']['ready_script']} {s['readiness']['value']} {timeout:.1f}")
            tag(c, 'repair', 'stop')
            tag(c, 'waitfor', 0, wait='exits', polltime='100:' + str(math.ceil((timeout+1)*1000)))
    ET.indent(root, space='  ')
    return ET.tostring(root, encoding='unicode', short_empty_elements=False) + '\n'


def buildfile_fragment(config):
    services = validate(config); rt = config['runtime']
    lines = ['# BootWave BSP/SLM bridge; merge only into a reviewed BSP.',
             '# Prerequisite: slogger2 running; declared binaries, shell and config installed.',
             '[+script] .bootwave = {']
    for s in services.values():
        if s['phase'] == 'bsp':
            suffix = ' &' if s['launch_mode'] == 'background' else ''
            lines.append('    ' + ' '.join([s['executable'], *s['args']]) + suffix)
    # config is positional, not a nonexistent -f option.
    lines += [f"    {rt['slm']} -r stop {rt['slm_config']} &", '}']
    return '\n'.join(lines) + '\n'


def parse_slm(text):
    """Parse documented literal SLM: names without namespace expansion."""
    stack = []; roots = []; parser = expat.ParserCreate()
    def start(name, attrs):
        node = {'tag': name, 'attrs': attrs, 'text': '', 'children': []}
        (stack[-1]['children'] if stack else roots).append(node); stack.append(node)
    parser.StartElementHandler = start; parser.EndElementHandler = lambda name: stack.pop()
    def data(value):
        if stack: stack[-1]['text'] += value
    parser.CharacterDataHandler = data
    parser.StartDoctypeDeclHandler = lambda *a: (_ for _ in ()).throw(ValueError('No external XML entities supported'))
    parser.Parse(text, True)
    if len(roots) != 1 or roots[0]['tag'] != 'SLM:system': raise ValueError('SLM system required')
    out = {}
    for c in roots[0]['children']:
        if c['tag'] != 'SLM:component': raise ValueError('Replay supports explicit components only')
        name = c['attrs']['name']
        if name in out: raise ValueError('Duplicate generated component')
        fields = {x['tag']: x for x in c['children'] if x['tag'] != 'SLM:depend'}
        out[name] = {'command': fields['SLM:command']['text'].strip(),
                     'args': shlex.split(fields['SLM:args']['text']),
                     'dependencies': [x['text'].strip() for x in c['children'] if x['tag'] == 'SLM:depend'],
                     'wait': fields['SLM:waitfor']['attrs']['wait'],
                     'expected_exit': fields['SLM:waitfor']['text'].strip(),
                     'timeout_s': float(fields['SLM:waitfor']['attrs'].get('polltime','100:50000').split(':')[1])/1000,
                     'repair': fields['SLM:repair']['text'].strip()}
    for c in out.values():
        if any(d not in out for d in c['dependencies']): raise ValueError('Missing XML prerequisite')
    return out


def replay_generated(config, xml, fragment, helper, actual=None):
    """Virtual-time replay of emitted commands/dependencies, not an SLM/QNX emulator.

    actual injects delays, missing paths and command failures. Input commands are
    never run. Path-gate exit 0 is modelled only after successful readiness.
    """
    services = validate(config); actual = actual or {}; components = parse_slm(xml)
    if helper != ready_helper(config): raise ValueError('Unsupported gate-helper contract')
    expected_ids={s['id'] for s in services.values() if s['phase']=='slm'}|{ready_id(s) for s in services.values() if s['readiness']['kind']=='path'}
    if set(components)!=expected_ids: raise ValueError('Emitted component set mismatch')
    rt=config['runtime']
    for name,c in components.items():
        if name.startswith('bw_ready_'):
            source=services[name[len('bw_ready_'):]]
            expected_deps=[source['id']] if source['phase']=='slm' else []
            timeout=math.ceil(source['timeout_s']*10)/10
            expected_args=[rt['ready_script'],source['readiness']['value'],f'{timeout:.1f}']
            if c['command']!=rt['sh'] or c['args']!=expected_args or c['wait']!='exits':
                raise ValueError('Bad emitted path gate')
            if c['timeout_s']<=timeout:
                raise ValueError('SLM exit wait must allow the complete helper timeout')
        else:
            source=services[name]
            expected_deps=[ready_id(services[d]) for d in source['requires']]
            if c['command']!=source['executable'] or c['args']!=source['args']:
                raise ValueError('Bad emitted executable/args')
            expected_wait='exits' if source['readiness']['kind']=='exits' else 'none'
            if c['wait']!=expected_wait:
                raise ValueError('Emitted wait condition does not match declared readiness')
            if expected_wait=='exits' and c['timeout_s']!=math.ceil(source['timeout_s']*1000)/1000:
                raise ValueError('Emitted exit wait timeout does not match declared timeout')
        if c['dependencies']!=expected_deps or c['repair']!='stop': raise ValueError('Emitted dependency/repair contract mismatch')
        if c['wait']=='exits' and c['expected_exit']!='0': raise ValueError('Readiness gate must require successful exit 0')
    starts={};ready_at={};trace=[];events=[];now=0.
    expected_bsp={' '.join([s['executable'],*s['args']]):s for s in services.values() if s['phase']=='bsp'}
    saw_slm=False
    for line in fragment.splitlines():
        line=line.strip()
        if not line or line.startswith('#') or line.startswith('[+script]') or line=='}':continue
        bare=line[:-2] if line.endswith(' &') else line
        if bare==f"{rt['slm']} -r stop {rt['slm_config']}":
            if not line.endswith(' &'):raise ValueError('SLM must not block remaining boot script')
            saw_slm=True;break
        if bare not in expected_bsp:raise ValueError('Unknown emitted BSP command')
        s=expected_bsp.pop(bare);starts[s['id']]=now
        if (s['launch_mode']=='background')!=line.endswith(' &'):raise ValueError('Wrong BSP launch mode')
        trace.append({'kind':'bsp_launch','id':s['id'],'at_s':now});now+=s['launch_return_s']
    if not saw_slm or expected_bsp:raise ValueError('Missing emitted BSP/SLM launch')
    states={n:'pending' for n in components};slm_start=now
    def setting(s,key,default):return actual.get(s['id'],{}).get(key,default)
    def fail(name,at):
        states[name]='failed';trace.append({'kind':'failed','id':name,'at_s':at});changed=True
        while changed:
            changed=False
            for n in sorted(components):
                c=components[n]
                if states[n] not in ('failed','blocked') and any(states[d] in ('failed','blocked') for d in c['dependencies']):
                    states[n]='blocked';trace.append({'kind':'blocked','id':n,'at_s':at});changed=True
    while True:
        launched=False
        for n in sorted(components):
            c=components[n]
            if states[n]!='pending' or not all(states[d]=='ready' for d in c['dependencies']):continue
            launched=True;states[n]='active';trace.append({'kind':'slm_launch','id':n,'at_s':now})
            if n.startswith('bw_ready_'):
                s=services[n[len('bw_ready_'):]]
                due=starts[s['id']]+setting(s,'ready_after_s',s['ready_after_s']);timeout=float(c['args'][2])
                missing=setting(s,'missing_path',False) or setting(s,'command_failure',False)
                ok=not missing and due<=now+timeout
                heapq.heappush(events,(max(now,due) if ok else now+timeout,0 if not ok else 1,n,ok))
            else:
                s=services[n]
                starts[n]=now
                if setting(s,'command_failure',False):fail(n,now)
                elif c['wait']=='none':states[n]='ready';ready_at[n]=now
                elif c['wait']=='exits':
                    delay=setting(s,'ready_after_s',s['ready_after_s']);ok=delay<=c['timeout_s']
                    heapq.heappush(events,(now+min(delay,c['timeout_s']),0 if not ok else 1,n,ok))
                else:raise ValueError('Unsupported emitted wait condition')
        # Launch and wait="none" transitions consume no virtual time. Reach
        # their fixed point before an unrelated future event can advance now.
        if launched:continue
        if not events:break
        now=events[0][0]
        # Failure precedes readiness at an equal timestamp (the heap priority),
        # and the complete timestamp batch precedes the next launch closure.
        while events and events[0][0]==now:
            _,_,n,ok=heapq.heappop(events)
            if states[n]=='active':
                if ok:states[n]='ready';ready_at[n]=now;trace.append({'kind':'ready','id':n,'at_s':now})
                else:fail(n,now)
    for n in states:
        if states[n]=='pending':states[n]='blocked'
    service_ready={n:ready_at.get(ready_id(s)) for n,s in services.items()}
    success=all(states[ready_id(s)]=='ready' for s in services.values())
    return {'scope':'Virtual-time replay of emitted BSP lines, SLM XML and shell-gate contract; no target commands executed',
            'slm_start_s':slm_start,'starts_s':starts,'service_ready_s':service_ready,'component_states':states,
            'total_ready_s':max(service_ready.values()) if success else None,
            'first_useful_ready_s':service_ready[config['first_useful_application']],'trace':trace}


def replay_manual(config,actions,actual=None):
    """Blocking waits in a supplied manual order, with the same failure gates."""
    services=validate(config);actual=actual or {};starts={};ready={};failed=set();now=0.
    for action in actions:
        kind,n=action.split(':');s=services[n];obs=actual.get(n,{})
        if kind=='launch':
            if any(d in failed for d in s['requires']):failed.add(n);continue
            if not all(d in ready for d in s['requires']):raise ValueError('Manual launch before prerequisite readiness')
            starts[n]=now;now+=s['launch_return_s']
            if obs.get('command_failure'):failed.add(n)
        elif kind=='wait':
            if n in failed:continue
            if n not in starts:raise ValueError('Manual wait before launch')
            end=starts[n]+obs.get('ready_after_s',s['ready_after_s'])
            if obs.get('missing_path') or end>now+s['timeout_s']:now+=s['timeout_s'];failed.add(n)
            else:now=max(now,end);ready[n]=now
        else:raise ValueError('Unknown manual action')
    return {'scope':'Declared manual blocking-order model, not a target measurement','total_ready_s':max(ready.values()) if len(ready)==len(services) else None,
            'first_useful_ready_s':ready.get(config['first_useful_application']),'starts_s':starts,'ready_s':ready}


def generate(config):
    xml=slm_xml(config);fragment=buildfile_fragment(config);helper=ready_helper(config)
    plan={'qnx_version':'8.0','kind':'BSP/SLM bridge compiler; native SLM supplies launch/monitor semantics',
          'required_target_files':sorted({config['runtime'][x] for x in ('slm','sh','waitfor','ready_script','slm_config')}|{s['executable'] for s in config['services']}),
          'prerequisites':['slogger2 already running','fresh, meaningful readiness paths','reviewed BSP and available shared-library dependencies'],
          'replay':replay_generated(config,xml,fragment,helper)}
    if config.get('manual_actions'):plan['manual_reference']=replay_manual(config,config['manual_actions'])
    return {'bootwave.slm.xml':xml,'bootwave.build':fragment,'bootwave-ready.sh':helper,'plan.json':json.dumps(plan,indent=2)+'\n'}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('config',type=Path);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    config=json.loads(args.config.read_text(encoding='utf-8'));outputs=generate(config);args.output.mkdir(parents=True,exist_ok=True)
    for name,text in outputs.items():(args.output/name).write_text(text,encoding='utf-8',newline='\n')
    print(outputs['plan.json'])
