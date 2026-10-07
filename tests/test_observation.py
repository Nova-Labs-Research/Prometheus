"""Prepared claims/temp stores only; never OS observers or guest fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from prometheus_lab.lab_control.boundary import request_vm_operation, VMOperationsUnavailable
from prometheus_lab.lab_control.contracts import Context
from prometheus_lab.lab_control.controller_journal import ControllerJournal
from prometheus_lab.lab_control.durable import DurableLedger, StoreError, ZERO, _hash
from prometheus_lab.lab_control.observation import (
    Action, Binding, Challenge, Claim, ExternalAuthenticationUnavailable, Observation,
    MockContextConsent, ObservationContext, Received, RecordedEvent, TestPeer as Peer,
    authenticate_external_peer, canonical, replay,
)
from prometheus_lab.lab_control.observation_store import ObservationJournal
from test_controller import request
from test_phase2a_contract import clock, uid


def tick(ms):
    return clock(milliseconds=ms)


def context(**changes):
    values = dict(schema_version='observation-rehearsal-context-v1', store_id=uid(900),
        installation_id=uid(901), operation_id=uid(902), deployment_digest='a'*64,
        disk_id=uid(903), initial_disk_digest='b'*64, recovery_policy_digest='c'*64,
        session=Context(process_epoch=uid(2), origin=clock()), request=request(),
        peers=tuple(Peer(kind='test_only', role=role, instance_id=uid(20+i))
                    for i, role in enumerate(('observer','broker','watchdog','collector'))),
        freshness_ms=1000, lease_until_ms=5000)
    values.update(changes)
    draft=ObservationContext.model_construct(**values)
    values['consent']=MockContextConsent(kind='mock_context_consent',
        approval_id=values['request'].approval.approval_id, context_digest=draft.consent_digest())
    return ObservationContext(**values)


def pair(role='observer', state='off', start=10, number=1, sequence=1, ctx=None, fenced=False):
    ctx = ctx or context()
    peer = next(p for p in ctx.peers if p.role == role)
    category = dict(observer='host_lifecycle', broker='broker_quiescence',
                    watchdog='watchdog_status', collector='guest_claim')[role]
    claim = Claim(category=category, state=state, prior_broker_fenced=fenced)
    raw = canonical(claim)
    query = Challenge(kind='challenge', binding=ctx.binding(), challenge_id=uid(2000+number),
                      peer=peer, clock=tick(start), expires_ms=start+ctx.freshness_ms)
    report = Observation(schema_version='observation-rehearsal-v1', binding=ctx.binding(),
        observation_id=uid(1000+number), challenge_id=query.challenge_id, reporter_sequence=sequence,
        query_start_ms=start+1, query_end_ms=start+2, claim=claim,
        payload_digest=sha256(raw).hexdigest(), payload_bytes=len(raw))
    return RecordedEvent(event=query), RecordedEvent(event=Received(
        kind='received', peer=peer, observation=report, clock=tick(start+3)))


def action(name, ms):
    return RecordedEvent(event=Action(kind='action', action=name, clock=tick(ms)))


def launched(ctx=None):
    ctx = ctx or context()
    return (*pair(ctx=ctx), *pair('watchdog','armed',20,2,ctx=ctx),
            action('fence',30), action('dispatch',31))


WORKER = r'''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from test_observation import uid, action, context, launched
from prometheus_lab.lab_control.observation_store import ObservationJournal
mode = sys.argv[3]
target = sys.argv[4] if len(sys.argv) > 4 else 'interrupt'
if target == 'interrupt':
    store = ObservationJournal.reopen(Path(sys.argv[2]), uid(900))
else:
    store = ObservationJournal.create(Path(sys.argv[2]), context())
    for record in launched()[:4 if target == 'fence' else 5]:
        store.append(record, store.snapshot().head)
def checkpoint(name):
    if name == mode:
        os._exit(73)
store._checkpoint = checkpoint
store.append(action(target,40), store.snapshot().head)
'''


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'observation.sqlite'
        self.ctx = context()
        self.store = ObservationJournal.create(self.path, self.ctx)

    def append(self, *records, store=None):
        store = store or self.store
        result = store.snapshot()
        for record in records:
            result = store.append(record, result.head)
        return result

    def rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT seq,kind,body,digest FROM events ORDER BY seq').fetchall()

    def test_initial_context_is_durable_immutable_and_non_authorizing(self):
        row = self.rows()[0]
        self.assertEqual(row[1], 'observation_context')
        self.assertEqual(json.loads(row[2]), self.ctx.model_dump(mode='json'))
        result = self.store.snapshot()
        self.assertEqual(result.issues, ('missing_observations',))
        self.assertEqual(result.scenario_state, 'reconciliation_required')
        self.assertFalse(result.confers_authority)
        self.assertFalse(result.valid_completion)
        self.assertTrue(result.requires_external_quarantine)
        with self.assertRaises(FrozenInstanceError):
            result.head = 'x'
        with self.assertRaises(ValueError):
            result.context.freshness_ms = 1

    def test_exact_binding_fields_independently_reject_from_valid_baseline(self):
        query, received = pair()
        self.assertEqual(len(replay(self.ctx,(query,received)).observations),1)
        for name,value in self.ctx.binding().model_dump().items():
            with self.subTest(field=name):
                changed = received.event.observation.binding.model_copy(update={
                    name: 'f'*64 if isinstance(value,str) else uid(9999)})
                report = received.event.observation.model_copy(update={'binding':changed})
                event = received.event.model_copy(update={'observation':report})
                with self.assertRaises(ValueError):
                    replay(self.ctx,(query,RecordedEvent(event=event)))

    def test_context_changes_invalidate_old_queries_and_digests(self):
        variants = dict(installation_id=uid(999),operation_id=uid(999),disk_id=uid(999),
            deployment_digest='d'*64,initial_disk_digest='d'*64,recovery_policy_digest='d'*64,
            freshness_ms=999,lease_until_ms=4999,
            peers=tuple(p.model_copy(update={'instance_id':uid(80+i)}) for i,p in enumerate(self.ctx.peers)))
        for field,value in variants.items():
            with self.subTest(field=field):
                changed = context(**{field:value})
                self.assertNotEqual(changed.digest(),self.ctx.digest())
                with self.assertRaises(ValueError):
                    replay(changed,pair())

    def test_context_requires_consistent_mock_admission_and_roles(self):
        variants = [dict(request=request(watchdog_ready=False)),dict(lease_until_ms=0),
            dict(peers=self.ctx.peers[:-1]),dict(peers=self.ctx.peers+(self.ctx.peers[0],)),
            dict(peers=tuple(p.model_copy(update={'instance_id':uid(1)}) for p in self.ctx.peers)),
            dict(session=Context(process_epoch=uid(9),origin=clock()))]
        for change in variants:
            with self.subTest(change=tuple(change)):
                with self.assertRaises(ValueError):
                    context(**change)

    def test_real_authentication_and_execution_remain_unconditionally_unavailable(self):
        class Hostile:
            def __getattribute__(self,name):
                raise AssertionError('Input inspected')
        for call,error in [(authenticate_external_peer,ExternalAuthenticationUnavailable),
                           (request_vm_operation,VMOperationsUnavailable)]:
            with self.subTest(call=call.__name__):
                with self.assertRaises(error):
                    call(Hostile(),enabled=True,authenticated=True)
        for change in [dict(kind='authenticated'),dict(authenticated=True),dict(sid='fake')]:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    Peer.model_validate(self.ctx.peers[0].model_dump()|change)

    def test_peer_role_and_instance_cannot_be_supplied_by_report(self):
        query,received = pair()
        for peer in [self.ctx.peers[1],self.ctx.peers[0].model_copy(update={'instance_id':uid(500)})]:
            with self.subTest(peer=peer.role):
                with self.assertRaises(ValueError):
                    replay(self.ctx,(query,RecordedEvent(event=received.event.model_copy(update={'peer':peer}))))
        guest = pair('collector','finished')
        result = replay(self.ctx,guest)
        self.assertEqual(result.evidence_classifications,('guest_claim',))
        self.assertFalse(result.valid_completion)
        with self.assertRaises(ValueError):
            replay(self.ctx,(*guest,action('abandon',20)))

    def test_payload_digest_length_and_schema_cannot_be_bypassed(self):
        report = pair()[1].event.observation
        for change in [dict(payload_digest='f'*64),dict(payload_bytes=1),dict(schema_version='external-v1'),
                       dict(query_start_ms=20),dict(authenticated=True)]:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    Observation.model_validate(report.model_copy(update=change))
        with self.assertRaises(ValueError):
            Claim(category='guest_claim',state='off',prior_broker_fenced=False)
        with self.assertRaises(ValueError):
            Claim(category='host_lifecycle',state='off',prior_broker_fenced=True)

    def test_ordered_lifecycle_and_explicit_abandonment_never_mean_completion(self):
        timeline = (*launched(),*pair('observer','running',40,3,2),action('stop_requested',50),
            *pair('broker','settled',60,4,fenced=True),*pair('observer','off',70,5,3),action('abandon',80))
        result = self.append(*timeline)
        self.assertEqual(result.scenario_state,'abandoned_unverified')
        self.assertEqual(result.issues,())
        self.assertEqual(result.simulated_dispatches,1)
        self.assertFalse(result.watchdog_required_in_scenario)
        self.assertTrue(result.requires_external_quarantine)
        self.assertFalse(result.valid_completion)
        self.assertFalse(result.confers_authority)
        self.assertEqual(len(self.rows()),1+len(timeline))

    def test_off_after_dispatch_does_not_resolve_pending_or_unfenced_broker(self):
        for state,fenced in [('pending',True),('unknown',True),('settled',False)]:
            with self.subTest(state=state,fenced=fenced):
                events = (*launched(),*pair('broker',state,40,3,fenced=fenced),*pair('observer','off',50,4,2))
                view = replay(self.ctx,events)
                self.assertEqual(view.scenario_state,'dispatch_unknown')
                self.assertTrue(view.watchdog_required_in_scenario)
                with self.assertRaises(ValueError):
                    replay(self.ctx,(*events,action('abandon',60)))

    def test_recovery_requires_off_after_quiescence_and_after_last_effect(self):
        for tail in [(*pair('observer','off',40,3,2),*pair('broker','settled',50,4,fenced=True)),
                     (*pair('broker','settled',40,3,fenced=True),*pair('observer','off',50,4,2),action('stop_requested',60))]:
            with self.subTest(tail=len(tail)):
                with self.assertRaises(ValueError):
                    replay(self.ctx,(*launched(),*tail,action('abandon',70)))

    def test_identical_redelivery_is_read_only_even_with_lost_ack(self):
        query,received = pair()
        first = self.append(query,received)
        before = self.path.read_bytes()
        retry = RecordedEvent(event=received.event.model_copy(update={'clock':tick(2000)}))
        result = self.store.append(retry,first.head)
        self.assertEqual(result,first)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(len(self.rows()),3)

    def test_conflicting_duplicate_is_retained_and_quarantines(self):
        query,received = pair()
        changed = pair(state='running')[1]
        result = self.append(query,received,changed)
        self.assertEqual(len(result.observations),2)
        self.assertIn('conflicting_duplicate',result.issues)
        self.assertEqual(result.scenario_state,'quarantined')
        self.assertEqual(len(self.rows()),4)
        self.assertFalse(result.valid_completion)

    def test_missing_sequence_challenge_replay_and_overlapping_conflict(self):
        cases = [(*pair(),*pair(start=20,number=2,sequence=3)),
                 (*pair(),pair(state='running',number=1)[1]),
                 (*pair(),*pair(state='running',start=12,number=2,sequence=2))]
        # Third query is issued before first receipt: retain chronological ingress.
        q1,r1 = pair(); q2,r2 = pair(state='running',start=11,number=2,sequence=2)
        cases[2]=(q1,q2,r1,r2)
        for events,reason in zip(cases,['sequence_gap_or_replay','challenge_replay','contradictory_interval']):
            with self.subTest(reason=reason):
                result = replay(self.ctx,events)
                self.assertIn(reason,result.issues)
                self.assertFalse(result.valid_completion)

    def test_stale_interval_exact_deadline_and_future_samples_quarantine(self):
        query,received = pair()
        for changed in [received.event.model_copy(update={'clock':tick(1010)}),
                        received.event.model_copy(update={'observation':received.event.observation.model_copy(update={'query_end_ms':14})})]:
            with self.subTest(clock=changed.clock.monotonic_ms):
                result = replay(self.ctx,(query,RecordedEvent(event=changed)))
                self.assertIn('stale_or_invalid_interval',result.issues)
        valid = received.event.model_copy(update={'clock':tick(1009)})
        self.assertEqual(replay(self.ctx,(query,RecordedEvent(event=valid))).issues,())

    def test_unexplained_running_or_stop_is_not_a_normal_transition(self):
        self.assertIn('running_unattributed',replay(self.ctx,pair(state='running')).issues)
        events = (*launched(),*pair('observer','running',40,3,2),*pair('observer','off',50,4,3))
        self.assertIn('unexplained_transition',replay(self.ctx,events).issues)

    def test_final_dispatch_check_rejects_expired_lease_and_keeps_watchdog(self):
        ctx=context(lease_until_ms=31)
        events=(*pair(ctx=ctx),*pair('watchdog','armed',20,2,ctx=ctx),action('fence',30),action('dispatch',31))
        result=replay(ctx,events)
        self.assertIn('dispatch_denied',result.issues)
        self.assertEqual(result.simulated_dispatches,0)
        self.assertTrue(result.watchdog_required_in_scenario)
        self.assertTrue(result.requires_external_quarantine)

    def test_approval_expiry_and_readiness_freshness_boundaries(self):
        ctx=context(freshness_ms=1000000,lease_until_ms=1000000)
        for ms,allowed in [(899999,True),(900000,False)]:
            with self.subTest(ms=ms):
                events=(*pair(ctx=ctx),*pair('watchdog','armed',20,2,ctx=ctx),action('fence',30),action('dispatch',ms))
                self.assertEqual(replay(ctx,events).simulated_dispatches,int(allowed))
        for ms,allowed in [(1011,True),(1012,False)]:
            with self.subTest(ms=ms):
                self.assertEqual(replay(self.ctx,(*launched()[:-1],action('dispatch',ms))).simulated_dispatches,int(allowed))

    def test_second_dispatch_and_fence_are_prohibited(self):
        for event in [action('dispatch',40),action('fence',40)]:
            with self.subTest(action=event.event.action):
                with self.assertRaises(ValueError):
                    replay(self.ctx,(*launched(),event))
        with self.assertRaises(ValueError):
            replay(self.ctx,(action('dispatch',1),))

    def test_interrupt_invalidates_challenges_and_never_restores_dispatch(self):
        q,r=pair(start=40,number=3,sequence=2)
        result=replay(self.ctx,(*launched(),q,action('interrupt',41),r))
        self.assertIn('challenge_replay',result.issues)
        result=replay(self.ctx,(*launched()[:-1],action('interrupt',31),action('dispatch',32)))
        self.assertEqual(result.simulated_dispatches,0)
        self.assertTrue(result.watchdog_required_in_scenario)
        events=(*launched(),action('interrupt',40),*pair('broker','settled',50,3,fenced=True),
                *pair('observer','off',60,4,2),action('abandon',70))
        self.assertEqual(replay(self.ctx,events).scenario_state,'abandoned_unverified')

    def test_reopen_preserves_uncertainty_and_never_resumes_dispatch(self):
        self.append(*launched()[:-1])
        reopened=ObservationJournal.reopen(self.path,uid(900))
        self.assertEqual(reopened.snapshot().scenario_state,'dispatch_unknown')
        with self.assertRaises(StoreError):
            reopened.append(action('dispatch',31),reopened.snapshot().head)
        self.assertEqual(self.store.snapshot().simulated_dispatches,0)

    def test_head_bound_reconciliation_race_cannot_drop_new_observation(self):
        self.append(*launched(),*pair('broker','settled',40,3,fenced=True),*pair('observer','off',50,4,2))
        old=self.store.snapshot().head
        q,r=pair('collector','finished',60,5)
        self.append(q,r)
        with self.assertRaises(StoreError):
            self.store.append(action('abandon',70),old)
        self.assertEqual(len(self.store.snapshot().observations),5)
        reopened=ObservationJournal.reopen(self.path,uid(900))
        self.append(action('interrupt',70),store=reopened)
        self.append(*pair('broker','settled',80,6,2,fenced=True),
                    *pair('observer','off',90,7,3),store=reopened)
        self.assertEqual(self.append(action('abandon',100),store=reopened).scenario_state,'abandoned_unverified')

    def test_concurrent_same_head_allows_one_append(self):
        head=self.store.snapshot().head
        def attempt():
            store=ObservationJournal.reopen(self.path,uid(900))
            try:
                store.append(action('interrupt',1),head)
                return 'committed'
            except StoreError:
                return 'closed'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _:attempt(),range(2))),['closed','committed'])
        self.assertEqual(len(self.rows()),2)

    def test_snapshot_is_read_only_and_prior_versions_are_not_migrated(self):
        before=self.path.read_bytes()
        self.store.snapshot()
        self.assertEqual(self.path.read_bytes(),before)
        for cls in [ControllerJournal,DurableLedger]:
            with self.subTest(cls=cls.__name__):
                with self.assertRaises(StoreError):
                    cls.reopen(self.path,uid(900))
        old=self.path.with_name('v2.sqlite')
        ControllerJournal.create(old,uid(900))
        with self.assertRaises(StoreError):
            ObservationJournal.reopen(old,uid(900))

    def test_missing_exclusive_and_wrong_identity_stores(self):
        with self.assertRaises(FileExistsError):
            ObservationJournal.create(self.path,self.ctx)
        missing=self.path.with_name('missing.sqlite')
        with self.assertRaises(StoreError):
            ObservationJournal.reopen(missing,uid(900))
        self.assertFalse(missing.exists())
        with self.assertRaises(StoreError):
            ObservationJournal.reopen(self.path,uid(901))

    def test_exception_before_and_after_commit_preserves_actual_rows(self):
        for point,expected in [('before_commit',1),('after_commit',2)]:
            with self.subTest(point=point):
                path=self.path.with_name(point+'.sqlite')
                store=ObservationJournal.create(path,self.ctx)
                def fault(name):
                    if name==point: raise OSError('injected')
                with patch.object(store,'_checkpoint',side_effect=fault):
                    with self.assertRaises(OSError):
                        store.append(action('interrupt',1),store.snapshot().head)
                with self.assertRaises(StoreError):
                    store.append(action('interrupt',2),store.snapshot().head)
                reopened=ObservationJournal.reopen(path,uid(900))
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],expected)
                self.assertFalse(reopened.snapshot().valid_completion)

    def test_actual_process_exit_before_and_after_commit(self):
        for point,count in [('before_commit',1),('after_commit',2)]:
            with self.subTest(point=point):
                path=self.path.with_name('crash-'+point+'.sqlite')
                ObservationJournal.create(path,self.ctx)
                result=subprocess.run([sys.executable,'-c',WORKER,str(Path(__file__).parent),str(path),point],
                                      capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,73,result.stderr)
                reopened=ObservationJournal.reopen(path,uid(900))
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],count)
                self.assertTrue(reopened.snapshot().requires_external_quarantine)

    def test_readback_failure_does_not_acknowledge_commit(self):
        head=self.store.snapshot().head
        with patch.object(self.store,'snapshot',side_effect=OSError('lost readback')):
            with self.assertRaises(OSError):
                self.store.append(action('interrupt',1),head)
        self.assertEqual(len(self.rows()),2)
        with self.assertRaises(StoreError):
            self.store.append(action('interrupt',2),self.store.snapshot().head)

    def test_corrupt_missing_and_rehashed_invalid_events_fail_closed(self):
        self.append(*pair())
        rows=self.rows()
        for mode in ['hash','missing','version','semantic']:
            with self.subTest(mode=mode):
                path=self.path.with_name(mode+'.sqlite')
                path.write_bytes(self.path.read_bytes())
                with closing(sqlite3.connect(path)) as db,db:
                    if mode=='hash': db.execute("UPDATE metadata SET head=?",('f'*64,))
                    elif mode=='missing': db.execute('DELETE FROM events WHERE seq=3')
                    elif mode=='version': db.execute('UPDATE metadata SET version=999')
                    else:
                        body=json.loads(rows[-1][2]);body['event']['observation']['binding']['approval_id']=str(uid(999))
                        wire=json.dumps(body,separators=(',',':'));digest=_hash(rows[-2][3],'observation_event',wire)
                        db.execute('UPDATE events SET body=?,digest=? WHERE seq=3',(wire,digest))
                        db.execute('UPDATE metadata SET head=?',(digest,))
                with self.assertRaises(StoreError):
                    ObservationJournal.reopen(path,uid(900))

    def test_malformed_bytes_fail_before_mutation(self):
        before=self.path.read_bytes()
        for value in [b'{}',b'\xff',b'{"event":{},"event":{}}',b' '*65537,
                      b'{"event":{"kind":"action","authenticated":true}}']:
            with self.subTest(value=value[:40]):
                with self.assertRaises(ValueError):
                    self.store.append(value,self.store.snapshot().head)
                self.assertEqual(self.path.read_bytes(),before)
        self.append(pair()[0])  # malformed input does not poison a healthy handle

    def test_no_effect_or_authentication_adapter_is_called(self):
        with patch('subprocess.run',side_effect=AssertionError('process')),\
             patch('os.system',side_effect=AssertionError('shell')),\
             patch('socket.socket',side_effect=AssertionError('socket')),\
             patch('prometheus_lab.lab_control.boundary.request_vm_operation',side_effect=AssertionError('VM')),\
             patch('prometheus_lab.lab_control.observation.authenticate_external_peer',side_effect=AssertionError('auth')):
            self.assertEqual(self.append(*launched()).simulated_dispatches,1)

    def test_expanded_context_requires_new_bound_mock_consent(self):
        for name,value in dict(deployment_digest='d'*64,disk_id=uid(404),
            initial_disk_digest='d'*64,recovery_policy_digest='d'*64,
            freshness_ms=900,lease_until_ms=9000,installation_id=uid(444),operation_id=uid(445)).items():
            with self.subTest(field=name):
                changed=self.ctx.model_copy(update={name:value})
                with self.assertRaises(ValueError):
                    ObservationContext.model_validate(changed)
                rebound=context(**{name:value})
                self.assertNotEqual(rebound.consent.context_digest,self.ctx.consent.context_digest)
                self.assertFalse(replay(rebound,()).confers_authority)
        wrong=self.ctx.consent.model_copy(update={'approval_id':uid(999)})
        with self.assertRaises(ValueError):
            ObservationContext.model_validate(self.ctx.model_copy(update={'consent':wrong}))

    def test_stop_before_dispatch_irrevocably_inhibits_dispatch(self):
        events=(*launched()[:-1],action('stop_requested',31),action('dispatch',32))
        result=self.append(*events)
        self.assertEqual(result.simulated_dispatches,0)
        self.assertIn('dispatch_denied',result.issues)
        self.assertTrue(result.watchdog_required_in_scenario)
        with self.assertRaises(ValueError):
            replay(self.ctx,(*events,action('stop_requested',33)))

    def test_failed_dispatch_can_only_be_abandoned_with_fresh_recovery_claims(self):
        ctx=context(lease_until_ms=31)
        events=(*pair(ctx=ctx),*pair('watchdog','armed',20,2,ctx=ctx),action('fence',30),action('dispatch',31))
        with self.assertRaises(ValueError):
            replay(ctx,(*events,*pair('observer','off',40,3,2,ctx=ctx),action('abandon',50)))
        result=replay(ctx,(*events,*pair('broker','settled',40,3,ctx=ctx,fenced=True),
                          *pair('observer','off',50,4,2,ctx=ctx),action('abandon',60)))
        self.assertEqual(result.scenario_state,'abandoned_unverified')
        self.assertIn('dispatch_denied',result.issues)
        self.assertTrue(result.requires_external_quarantine)
        self.assertEqual(result.actual_vm_state,'unknown')
        self.assertFalse(result.valid_completion)

    def test_clean_later_samples_cannot_clear_conflict_or_missing_sequence(self):
        clean_tail=(*pair('broker','settled',40,4,fenced=True),*pair('observer','off',50,5,2),action('abandon',60))
        self.assertEqual(replay(self.ctx,(*pair(),*clean_tail)).scenario_state,'abandoned_unverified')
        cases=[((*pair(),pair(state='running')[1]),2),
               ((*pair(),*pair(start=20,number=2,sequence=3)),4)]
        for bad,sequence in cases:
            with self.subTest(kind=len(bad)):
                tail=(*pair('broker','settled',40,4,fenced=True),*pair('observer','off',50,5,sequence),action('abandon',60))
                with self.assertRaises(ValueError):
                    replay(self.ctx,(*bad,*tail))

    def test_reopened_writes_require_interrupt_and_fence_previous_owner(self):
        self.append(*launched()[:-1])
        reopened=ObservationJournal.reopen(self.path,uid(900))
        q,_=pair(start=40,number=3,sequence=2)
        before=self.path.read_bytes()
        with self.assertRaises(StoreError):
            reopened.append(q,reopened.snapshot().head)
        self.assertEqual(self.path.read_bytes(),before)
        reopened=ObservationJournal.reopen(self.path,uid(900))
        self.append(action('interrupt',31),store=reopened)
        result=self.append(action('dispatch',32))
        self.assertEqual(result.simulated_dispatches,0)
        self.assertIn('dispatch_denied',result.issues)

    def test_pure_and_durable_duplicate_clock_behavior_match(self):
        q,r=pair()
        retry=RecordedEvent(event=r.event.model_copy(update={'clock':tick(9999)}))
        q2,r2=pair(start=20,number=2,sequence=2)
        pure=replay(self.ctx,(q,r,retry,q2,r2))
        durable=self.append(q,r,retry,q2,r2)
        self.assertEqual(pure.events,durable.events)
        self.assertEqual(pure.observations,durable.observations)
        self.assertEqual(pure.issues,durable.issues)
        self.assertEqual(len(self.rows()),5)

    def test_fence_and_dispatch_process_crashes_keep_exact_committed_intent(self):
        for target,base in [('fence',5),('dispatch',6)]:
            for point,extra in [('before_commit',0),('after_commit',1)]:
                with self.subTest(target=target,point=point):
                    path=self.path.with_name(target+'-'+point+'.sqlite')
                    result=subprocess.run([sys.executable,'-c',WORKER,str(Path(__file__).parent),
                        str(path),point,target],capture_output=True,text=True,timeout=20)
                    self.assertEqual(result.returncode,73,result.stderr)
                    reopened=ObservationJournal.reopen(path,uid(900))
                    view=reopened.snapshot()
                    with closing(sqlite3.connect(path)) as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0],base+extra)
                    self.assertEqual(view.simulated_dispatches,int(target=='dispatch' and extra==1))
                    self.assertTrue(view.requires_external_quarantine)
                    with self.assertRaises(StoreError):
                        reopened.append(action('dispatch',50),view.head)

    def test_store_capacity_failure_never_deletes_evidence(self):
        self.append(pair()[0])
        before=self.path.read_bytes()
        with patch('prometheus_lab.lab_control.observation_store.MAX_EVENTS',2):
            with self.assertRaises(StoreError):
                self.store.append(pair()[1],self.store.snapshot().head)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(len(self.rows()),2)

    def test_wrong_or_reused_challenge_and_clock_reversal_reject(self):
        q,r=pair()
        for events in [(r,),(q,q),(q,action('interrupt',9))]:
            with self.subTest(length=len(events)):
                with self.assertRaises(ValueError):
                    replay(self.ctx,events)
        for expires in [10,1011]:
            with self.subTest(expires=expires):
                bad=RecordedEvent(event=q.event.model_copy(update={'expires_ms':expires}))
                with self.assertRaises(ValueError):
                    replay(self.ctx,(bad,))

    def test_recovery_freshness_exact_boundary_and_post_abandon_writes(self):
        events=(*pair('broker','settled',10,1,fenced=True),*pair('observer','off',20,2))
        self.assertEqual(replay(self.ctx,(*events,action('abandon',1011))).scenario_state,'abandoned_unverified')
        with self.assertRaises(ValueError):
            replay(self.ctx,(*events,action('abandon',1012)))
        with self.assertRaises(ValueError):
            replay(self.ctx,(*events,action('abandon',30),action('interrupt',40)))

    def test_whole_store_rollback_is_not_detectable_authenticity(self):
        earlier=self.path.read_bytes()
        self.append(*launched())
        self.path.write_bytes(earlier)
        reopened=ObservationJournal.reopen(self.path,uid(900))
        self.assertEqual(reopened.snapshot().simulated_dispatches,0)
        self.assertFalse(reopened.snapshot().confers_authority)
        self.assertTrue(reopened.snapshot().requires_external_quarantine)

    def test_delayed_sample_cannot_hide_conflict_with_older_history(self):
        q,received=pair(start=1,number=5,sequence=4)
        prefix=(q,*launched(),*pair('observer','running',40,3,2),action('stop_requested',50),
                *pair('observer','off',70,4,3))
        for start,end,now,conflict in [(81,82,83,False),(41,43,80,True)]:
            with self.subTest(conflict=conflict):
                report=received.event.observation.model_copy(update={'query_start_ms':start,'query_end_ms':end})
                event=RecordedEvent(event=received.event.model_copy(update={'observation':report,'clock':tick(now)}))
                view=replay(self.ctx,(*prefix,event))
                self.assertEqual('contradictory_interval' in view.issues,conflict)
                self.assertEqual('sample_order_reversal' in view.issues,conflict)
                self.assertFalse(view.valid_completion)
