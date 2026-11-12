"""Host-only structural, output-replay and optional POSIX helper checks."""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import runpy
import subprocess

ROOT=Path(__file__).resolve().parent
q=runpy.run_path(str(ROOT/'bootwave.py'))
base=json.loads((ROOT/'example.json').read_text(encoding='utf-8'))


def replay(config, actual=None):
    return q['replay_generated'](config,q['slm_xml'](config),q['buildfile_fragment'](config),q['ready_helper'](config),actual)


def check_configuration_drift():
    """A stale handwritten gate and an edited exit contract cannot pass replay."""
    updated = copy.deepcopy(base)
    updated['services'][2]['readiness']['value'] = '/dev/screen-v2'
    regenerated = q['generate'](updated)
    assert '/dev/screen-v2 5.0' in regenerated['bootwave.slm.xml']
    old_handwritten_xml = (ROOT/'manual_reference.slm.xml').read_text(encoding='utf-8')
    updated_handwritten_xml = old_handwritten_xml.replace('/dev/screen 5.0', '/dev/screen-v2 5.0', 1)
    updated_manual = q['replay_generated'](
        updated, updated_handwritten_xml, regenerated['bootwave.build'], regenerated['bootwave-ready.sh'])
    assert updated_manual['service_ready_s'] == replay(updated)['service_ready_s']

    exit_case = copy.deepcopy(base)
    exit_case['manual_actions'] = []
    exit_case['services'][3].update(executable='/sbin/if_up', args=['-p','en0'], readiness={'kind':'exits','value':0})
    exit_case['services'][4]['requires'] = ['splash']
    exit_outputs = q['generate'](exit_case)
    exit_wait = '<SLM:waitfor wait="exits" polltime="100:5000">0</SLM:waitfor>'
    assert exit_wait in exit_outputs['bootwave.slm.xml']
    weakened = exit_outputs['bootwave.slm.xml'].replace(exit_wait, '<SLM:waitfor wait="none"></SLM:waitfor>', 1)
    shortened = exit_outputs['bootwave.slm.xml'].replace(exit_wait, exit_wait.replace('100:5000', '100:200'), 1)
    expected_start = replay(exit_case)['starts_s']['logger']
    assert math.isclose(expected_start, 1.14)
    cases = (
        ('stale_manual_path', updated, regenerated, old_handwritten_xml, 'Bad emitted path gate'),
        ('exit_wait_replaced_by_none', exit_case, exit_outputs, weakened, 'Emitted wait condition'),
        ('exit_wait_timeout_changed', exit_case, exit_outputs, shortened, 'Emitted exit wait timeout'),
    )
    rejected = []
    for name, config, outputs, xml, expected_error in cases:
        try:
            q['replay_generated'](config, xml, outputs['bootwave.build'], outputs['bootwave-ready.sh'])
        except ValueError as error:
            assert expected_error in str(error), (name, error)
            rejected.append(name)
        else:
            raise AssertionError('Accepted ' + name)
    return {'rejected': rejected, 'updated_manual_matches_regenerated': True,
            'dependent_starts_after_exit_s': round(expected_start, 2),
            'scope': 'Synthetic host configuration change; no human timing or target runtime measured'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--host-shell');parser.add_argument('--scratch',type=Path);parser.add_argument('--receipt',type=Path)
    args=parser.parse_args();outputs=q['generate'](base);normal=replay(base)
    assert '/sbin/screen\n' in outputs['bootwave.build'] and 'screen &' not in outputs['bootwave.build']
    assert '\n    if ' not in outputs['bootwave.build'] and '-f ' not in outputs['bootwave.build']
    assert normal['total_ready_s']==1.52 and abs(normal['first_useful_ready_s']-1.14)<1e-9
    for name,text in outputs.items():
        assert (ROOT/'example_output'/name).read_bytes()==text.encode('utf-8'), 'Regenerate stale output: '+name
    manual=q['replay_manual'](base,base['manual_actions'])
    hand_slm=q['replay_generated'](base,(ROOT/'manual_reference.slm.xml').read_text(),outputs['bootwave.build'],outputs['bootwave-ready.sh'])
    assert math.isclose(manual['total_ready_s'],normal['total_ready_s'])
    assert math.isclose(manual['first_useful_ready_s'],normal['first_useful_ready_s'])
    assert hand_slm['service_ready_s']==normal['service_ready_s']
    configuration_drift = check_configuration_drift()
    rejected=[]
    for flaw in ('cycle','unsafe','screen','modifier','environment','raw_prefix','raw_env','interface','version','slm_daemon','slogger2'):
        bad=copy.deepcopy(base)
        if flaw=='cycle':bad['services'][3]['requires']=['logger'];bad['services'][4]['requires']=['splash']
        if flaw=='unsafe':bad['services'][0]['parallel_safe']=False
        if flaw=='screen':bad['services'][2]['launch_mode']='background'
        if flaw=='modifier':bad['services'][2]['modifiers']={'pri':10}
        if flaw=='environment':bad['services'][2]['env']={'FOO':'bar'}
        if flaw=='raw_prefix':bad['services'][2]['command']='[pri=10] screen'
        if flaw=='raw_env':bad['services'][2]['command']='FOO=bar screen'
        if flaw=='interface':bad['services'][0]['readiness']={'kind':'interface','value':'en0'}
        if flaw=='version':bad['qnx_version']='7.1'
        if flaw=='slm_daemon':bad['services'][3]['launch_mode']='self_daemon'
        if flaw=='slogger2':bad['runtime']['slogger2_already_running']=False
        try:q['generate'](bad)
        except ValueError:rejected.append(flaw)
        else:raise AssertionError('Accepted '+flaw)
    for xml in (outputs['bootwave.slm.xml'].replace('>bw_ready_screen<','>bw_ready_serial<',1),outputs['bootwave.slm.xml'].replace('>0</SLM:waitfor>','>1</SLM:waitfor>',1)):
        try:q['replay_generated'](base,xml,outputs['bootwave.build'],outputs['bootwave-ready.sh'])
        except ValueError:pass
        else:raise AssertionError('Changed generated dependency/success contract accepted')
    missing=replay(base,{'storage':{'missing_path':True}})
    assert 'logger' not in missing['starts_s'] and missing['total_ready_s'] is None
    assert missing['first_useful_ready_s']==normal['first_useful_ready_s']
    failed=replay(base,{'storage':{'command_failure':True}})
    assert 'logger' not in failed['starts_s'] and failed['component_states']['logger']=='blocked'
    follower=copy.deepcopy(base);follower['manual_actions']=[]
    child=copy.deepcopy(follower['services'][3]);child.update(id='client',executable='/usr/bin/demo-client',requires=['splash'])
    child['readiness']['value']='/dev/demo-client-ready';follower['services'].append(child)
    command_failed=replay(follower,{'splash':{'command_failure':True}})
    assert 'client' not in command_failed['starts_s'] and command_failed['first_useful_ready_s'] is None
    inverse=copy.deepcopy(base);inverse['manual_actions']=[];inverse['services']=[];inverse['first_useful_application']='C'
    for name,phase,deps,delay in [('A','bsp',[],.1),('B','bsp',[],.2),('C','slm',['B'],1.)]:
        inverse['services'].append({'id':name,'phase':phase,'requires':deps,'ready_after_s':delay,'timeout_s':6.,'launch_return_s':0.,'parallel_safe':True,'executable':'/usr/bin/demo-'+name,'args':[],'launch_mode':'background' if phase=='bsp' else 'foreground','readiness':{'kind':'path','value':'/dev/demo-'+name}})
    observed={'A':{'ready_after_s':4.}}
    inverted=replay(inverse,observed)
    good=q['replay_manual'](inverse,['launch:A','launch:B','wait:B','launch:C','wait:C','wait:A'],observed)
    old_order=q['replay_manual'](inverse,['launch:A','launch:B','wait:A','wait:B','launch:C','wait:C'],observed)
    assert inverted['total_ready_s']==good['total_ready_s']==4. and inverted['first_useful_ready_s']==good['first_useful_ready_s']==1.2
    assert old_order['total_ready_s']==5. and old_order['first_useful_ready_s']==5.
    # SLM components may be declared in any XML order. Exercise both the normal
    # workload and timings where a skipped zero-time closure used to delay a gate.
    permutation_rng=random.Random(20261111)
    permutation_cases=[('normal_five',base,{}),
                       ('early_serial',base,{'serial':{'ready_after_s':.1}}),
                       ('slow_splash',base,{'splash':{'ready_after_s':2.}}),
                       ('time_reversal',inverse,observed),
                       ('simultaneous_ready',inverse,{'A':{'ready_after_s':1.2}}),
                       ('missing_path',base,{'storage':{'missing_path':True}}),
                       ('managed_failure',follower,{'splash':{'command_failure':True}})]
    invariant_fields=('starts_s','service_ready_s','first_useful_ready_s','total_ready_s','component_states')
    for label,config,actual in permutation_cases:
        xml=q['slm_xml'](config);fragment=q['buildfile_fragment'](config);helper=q['ready_helper'](config)
        expected=q['replay_generated'](config,xml,fragment,helper,actual)
        pieces=re.findall(r'  <SLM:component.*?</SLM:component>',xml,re.S)
        assert len(pieces)==len(q['parse_slm'](xml))
        orders=[pieces[::-1]]
        for _ in range(100):
            shuffled=pieces[:];permutation_rng.shuffle(shuffled);orders.append(shuffled)
        for shuffled in orders:
            candidate='<SLM:system>\n'+'\n'.join(shuffled)+'\n</SLM:system>\n'
            current=q['replay_generated'](config,candidate,fragment,helper,actual)
            assert all(current[k]==expected[k] for k in invariant_fields),label
    exit_case=copy.deepcopy(base);exit_case['manual_actions']=[]
    exit_case['services'][3].update(executable='/sbin/if_up',args=['-p','en0'],readiness={'kind':'exits','value':0})
    assert replay(exit_case)['component_states']['splash']=='ready'
    assert replay(exit_case,{'splash':{'command_failure':True}})['component_states']['splash']=='failed'
    rng=random.Random(20261110)
    for _ in range(100):
        c=copy.deepcopy(base);c['manual_actions']=[]
        for i,s in enumerate(c['services']):
            s['ready_after_s']=rng.uniform(.1,2.);s['launch_return_s']=rng.uniform(0,.02)
            if s['phase']=='slm':s['requires']=[p['id'] for p in c['services'][:i] if rng.random()<.5]
        result=replay(c)
        for s in c['services']:
            assert all(result['starts_s'][s['id']]>=result['service_ready_s'][d]-1e-9 for d in s['requires'])
        assert result['total_ready_s']<=sum(s['ready_after_s']+s['launch_return_s'] for s in c['services'])
    host_shell={'status':'NOT_RUN','scope':'Optional host shell with stub waitfor, never QNX waitfor'}
    if args.host_shell:
        if not args.scratch or not any((p/'.codex-task-temp').exists() for p in [args.scratch,*args.scratch.parents]):raise ValueError('Marked CodexScratch required')
        args.scratch.mkdir(parents=True,exist_ok=True)
        stub=args.scratch/'waitfor-stub.sh';stub.write_text('#!/bin/sh\nexit "${BOOTWAVE_TEST_WAIT_STATUS:-0}"\n',encoding='utf-8',newline='\n')
        as_posix=lambda p:'/'+p.resolve().drive[0].lower()+p.resolve().as_posix()[2:]
        variant=copy.deepcopy(base);variant['runtime']['waitfor']=as_posix(stub)
        helper=args.scratch/'bootwave-ready-host.sh';helper.write_text(q['ready_helper'](variant),encoding='utf-8',newline='\n')
        present=args.scratch/'ready';present.write_text('ready');absent=args.scratch/'absent'
        results=[]
        for path,status,expected in [(present,0,0),(absent,0,1),(present,7,1)]:
            env=dict(os.environ,BOOTWAVE_TEST_WAIT_STATUS=str(status))
            r=subprocess.run([args.host_shell,str(helper),as_posix(path),'.1'],env=env,capture_output=True,text=True,timeout=10)
            assert r.returncode==expected,(r.returncode,r.stderr);results.append(r.returncode)
        r=subprocess.run([args.host_shell,str(helper)],capture_output=True,timeout=10);assert r.returncode==64
        host_shell={'status':'PASS','cases':'present=0, absent despite stub success=1, stub error despite present=1, invalid arity=64','scope':'Actual host POSIX shell; waitfor substituted by controlled exit-status stub. No QNX binary run.'}
    result={'status':'PASS','scope':'Host structural and deterministic output replay; not mkifs or QNX/SLM runtime validation',
            'configuration_drift':configuration_drift,
            'rejection_cases':rejected,'random_DAGs':100,'output_bytes_match':True,
            'XML_order_regressions':{'random_permutations_per_case':100,'explicit_reverse_per_case':1,'cases':[x[0] for x in permutation_cases],'total_permutations':707,'invariant_fields':list(invariant_fields)},
            'normal_comparison':{'BootWave':{'total_s':normal['total_ready_s'],'first_useful_s':normal['first_useful_ready_s']},'well_tuned_manual':{'total_s':manual['total_ready_s'],'first_useful_s':manual['first_useful_ready_s']},'handwritten_SLM':{'total_s':hand_slm['total_ready_s'],'first_useful_s':hand_slm['first_useful_ready_s']}},
            'time_inversion':{'BootWave_total_s':4.,'BootWave_first_useful_s':1.2,'well_tuned_manual_total_s':4.,'old_blocking_order_total_s':5.},
            'failure_tests':'Missing path blocks dependent logger but splash progresses; early command failure blocks logger; managed command failure blocks client',
            'host_shell':host_shell,'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in ['bootwave.py','verify.py','example.json','manual_reference.slm.xml']}}
    if args.receipt:args.receipt.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
