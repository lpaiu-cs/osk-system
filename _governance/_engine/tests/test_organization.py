"""Boundary regressions for persistent organization; each case owns a subprocess vault."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ENGINE = Path(__file__).resolve().parent.parent
BOOT = """
import json, sys
from pathlib import Path
from osk import core, contract, graph, write, validate, organization, distillation
validate.make_mini_vault(core.ROOT)
def node(name, body="Reusable evidence", edges=None):
    out = write.create_node(name, name, body, "gpt-6-astra", space="= Scope/W1", edges=edges)
    assert out["ok"], out
    wire("W1", name)
    return out
def wire(hub, name):
    idx=graph.Index(); p=write._live_locate(hub,idx); n=contract.parse(p)
    if name not in n.wikilinks():
        write.update_node(hub, body=n.body.rstrip()+chr(10)+"- [["+name+"]]",expect_hash=core.sha256_file(p))
def rejected(fn):
    try: fn()
    except (ValueError, write.WriteError): return
    raise AssertionError("invalid completion was accepted")
"""


class OrganizationTests(unittest.TestCase):
    def case(self, code):
        with tempfile.TemporaryDirectory(prefix="osk-organization-test-") as d:
            env=dict(os.environ, OSK_VAULT_ROOT=d, PYTHONPATH=str(ENGINE), TEMP=d, TMP=d)
            r=subprocess.run([sys.executable,"-B","-c",BOOT+textwrap.dedent(code)],cwd=d,env=env,
                             capture_output=True,text=True,encoding="utf-8",timeout=90,
                             creationflags=0x08000000 if os.name=="nt" else 0)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)

    def test_reference_roles_survive_restart_and_only_individual_links_can_be_retained(self):
        self.case("""
            a=node("A", "A fact with [[future question]]", {"derived-from":"docs/missing.md"})
            assert a["reference_review"]["status"]=="pending"
            assert any("[derived-from]" in x for x in graph.dangling_refs(graph.Index()))
            job=organization.plan("W1")
            intent=[{"id":a["id"],"relation":"Link","ref":"future question","reason":"Explicit open question"}]
            current=organization.snapshot("W1")
            rejected(lambda: organization.review(job["key"],"W1","complete","Keep question",after=current["snapshot"],intentional=intent))
            write.update_node("A",remove_edges={"derived-from":"docs/missing.md"})
            current=organization.snapshot("W1")
            organization.review(job["key"],"W1","complete","Bad source removed, question remains",after=current["snapshot"],intentional=intent)
            assert organization.plan("W1")["status"]=="complete"
            import subprocess,os
            r=subprocess.run([sys.executable,"-B","-c","from osk import organization; assert organization.plan('W1')['status']=='complete'"],env=os.environ,capture_output=True)
            assert r.returncode==0,r.stderr
            write.update_node("A",old_text="A fact",new_text="A revised fact")
            assert organization.plan("W1")["key"]!=job["key"]
        """)

    def test_source_navigation_and_raw_round_are_not_knowledge_nodes(self):
        self.case("""
            rp=core.ROOT/"= Scope/W1/_raw/evidence.md"; rp.parent.mkdir(parents=True,exist_ok=True)
            rp.write_text("## 1"+chr(10)+"User evidence",encoding="utf-8")
            a=node("A","See [[= Scope/W1/_raw/evidence.md#1]]",{"derived-from":"[[= Scope/W1/_raw/evidence.md]]"})
            items=a["reference_review"]["items"]
            assert {i["issue"] for i in items}=={"source_navigation","raw_round_required"},items
            assert any(i["ref"].endswith("#1") for i in items)
            assert not any(p==rp for p,k in graph.Index().nodes.values())
            old=core.sha256_file(rp)
            write.update_node("A",add_edges={"derived-from":"[[= Scope/W1/_raw/evidence.md#1]]"},remove_edges={"derived-from":"[[= Scope/W1/_raw/evidence.md]]"})
            assert core.sha256_file(rp)==old
        """)

    def test_real_hub_placement_and_identity_preservation(self):
        self.case("""
            a=node("A"); b=node("B"); job=organization.plan("W1")
            write.create_node("Branch","Branch","Two related facts","gpt-6-astra",space="= Scope/W1/Branch")
            wire("W1","Branch")
            moved=write.move_nodes(["A","B"],"= Scope/W1/Branch"); assert moved["ok"]
            now=organization.snapshot("W1")
            rejected(lambda: organization.review(job["key"],"W1","complete","Moved",after=now["snapshot"]))
            wire("Branch","A"); wire("Branch","B")
            write.update_node("W1",old_text="- [[A]]",new_text="")
            write.update_node("W1",old_text="- [[B]]",new_text="")
            now=organization.snapshot("W1")
            assert not now["issues"],now["issues"]
            organization.review(job["key"],"W1","complete","One meaningful branch; original IDs retained",after=now["snapshot"])
            assert organization.status(job)["status"]=="complete"
            path=write._live_locate(a["id"],graph.Index()); path.unlink()
            assert organization.status(job)["status"]=="pending"
        """)

    def test_missing_intermediate_hub_is_found_without_local_state(self):
        self.case("""
            write.create_node("Gap","Gap","Intermediate entrance","gpt-6-astra",space="= Scope/W1/Gap")
            write.create_node("Child","Child","Nested entrance","gpt-6-astra",space="= Scope/W1/Gap/Child")
            write.create_node("A","A","Retained nested knowledge","gpt-6-astra",space="= Scope/W1/Gap/Child")
            wire("Child","A")
            gap=write._live_locate("Gap",graph.Index()); original=gap.read_bytes(); gap.unlink()
            assert not organization._state_path().exists()
            job=organization.plan("W1")
            assert any(i["path"]=="= Scope/W1/Gap" and i["hub"] is None for i in job["issues"]),job
            rejected(lambda: organization.review(job["key"],"W1","complete","Disconnected subtree",after=job["snapshot"]))
            gap.write_bytes(original)
            wire("W1","Gap"); wire("Gap","Child")
            now=organization.snapshot("W1")
            assert not now["issues"],now["issues"]
            organization.review(job["key"],"W1","complete","Complete ancestor hub chain",after=now["snapshot"])
            assert organization.plan("W1")["status"]=="complete"
        """)

    def test_linked_worktrees_share_lock_but_not_organization_state(self):
        self.case("""
            import os,subprocess
            node("Shared")
            def git(*args):
                r=subprocess.run(["git",*args],cwd=core.ROOT,capture_output=True,text=True)
                assert r.returncode==0,r.stdout+r.stderr
            git("init","--quiet")
            git("add",".")
            git("-c","user.name=Fixture","-c","user.email=fixture@example.invalid","commit","--quiet","-m","Fixture")
            linked=core.ROOT/"linked"
            git("worktree","add","--quiet","--detach",str(linked),"HEAD")
            only=node("OnlyHere"); job=organization.plan("W1")
            before=organization._state_path().read_bytes()
            child=("import json; from osk import core,organization; "
                   "job=organization.plan('W1'); "
                   "assert not job.get('missing_ids'), job; "
                   "organization.review(job['key'],'W1','complete','This checkout only',after=job['snapshot']); "
                   "print(json.dumps({'state':str(organization._state_path()),'lock':str(core.local_lock_path('osk-mutation.lock'))}))")
            r=subprocess.run([sys.executable,"-B","-c",child],cwd=linked,
                             env=dict(os.environ,OSK_VAULT_ROOT=str(linked)),capture_output=True,text=True)
            assert r.returncode==0,r.stdout+r.stderr
            result=json.loads(r.stdout)
            assert result["state"]!=str(organization._state_path())
            assert result["lock"]==str(core.local_lock_path("osk-mutation.lock"))
            assert organization._state_path().read_bytes()==before
            assert organization.plan("W1")["key"]==job["key"]
        """)

    def test_partial_move_restarts_from_remaining_identical_nodes(self):
        self.case("""
            from unittest.mock import patch
            a=node("A"); b=node("B")
            write.create_node("Branch","Branch","Related facts","gpt-6-astra",space="= Scope/W1/Branch")
            job=organization.plan("W1")
            original=write._apply_move; calls=[]
            def injected(*args):
                calls.append(args[0])
                if len(calls)==2: raise OSError("injected second move failure")
                return original(*args)
            with patch("osk.write._apply_move",side_effect=injected):
                out=write.move_nodes(["A","B"],"= Scope/W1/Branch")
            assert not out["ok"] and out["remaining"]==["B"],out
            pending=organization.snapshot("W1")["pending_moves"]
            assert len(pending)==1 and pending[0]["remaining"][0]["intact"]
            out=write.move_nodes(out["remaining"],"= Scope/W1/Branch"); assert out["ok"],out
            assert not organization.snapshot("W1")["pending_moves"]
            for n in [a,b]:
                p=write._live_locate(n["id"],graph.Index()); assert core.sha256_file(p)==n["new_hash"]
            write.create_node("Other","Other","Another meaningful branch","gpt-6-astra",space="= Scope/W1/Other")
            assert write.move_nodes(["A"],"= Scope/W1/Other")["ok"]
            wire("W1","Branch"); wire("W1","Other"); wire("Branch","B"); wire("Other","A")
            write.update_node("W1",old_text="- [[A]]",new_text="")
            write.update_node("W1",old_text="- [[B]]",new_text="")
            now=organization.snapshot("W1")
            assert not now["issues"],now["issues"]
            assert not now["pending_moves"],now["pending_moves"]
            organization.review(job["key"],"W1","complete","Finished retry, then valid new placement",after=now["snapshot"])
            assert organization.plan("W1")["status"]=="complete"
        """)

    def test_relocation_keeps_history_and_rechecks_current_navigation(self):
        self.case("""
            s=node("Source","Observed mechanism")
            out=distillation.create_node({"key":"retained","sources":["Source"],"hub":"W1"},title="Conclusion",summary="Conclusion",body="Mechanism with scope limits",drafter="gpt-6-astra",space="= Scope/W1")
            assert out["distillation"]["status"]=="complete",out
            original=distillation._load("retained")
            write.create_node("Branch","Branch","Mechanism knowledge","gpt-6-astra",space="= Scope/W1/Branch")
            assert write.move_nodes(["Source","Conclusion"],"= Scope/W1/Branch")["ok"]
            proof=distillation.status("retained")
            assert proof["preservation"]["status"]=="complete" and proof["placement"]["status"]=="pending",proof
            proof=distillation.resume("retained",name="Conclusion")["distillation"]
            assert proof["status"]=="complete",proof
            assert distillation._load("retained")==original
            write.update_node("Source",old_text="Observed mechanism",new_text="Contrary measurement")
            assert distillation.status("retained")["preservation"]["status"]=="pending"
        """)

    def test_single_move_adapter_preserves_partial_failure_response(self):
        self.case("""
            from unittest.mock import patch
            a=node("A")
            write.create_node("Branch","Branch","Related facts","gpt-6-astra",space="= Scope/W1/Branch")
            with patch("osk.write._apply_move",side_effect=OSError("injected I/O failure")):
                failed=write.move_node("A","= Scope/W1/Branch")
            assert not failed["ok"] and failed["moved"]==[] and failed["remaining"]==["A"],failed
            assert core.sha256_file(write._live_locate(a["id"],graph.Index()))==a["new_hash"]
            assert write.move_node("A","= Scope/W1/Branch")["ok"]
            assert not organization.snapshot("W1")["pending_moves"]
        """)

    def test_cas_review_and_graph_settings_preserve_existing_preferences(self):
        self.case("""
            node("A"); job=organization.plan("W1"); old=organization.snapshot("W1")
            write.update_node("A",old_text="Reusable evidence",new_text="A later observation")
            rejected(lambda: organization.review(job["key"],"W1","complete","Reviewed earlier bytes",after=old["snapshot"]))
            sys.path.insert(0,str(Path(organization.__file__).resolve().parents[1]/"scripts"))
            import configure_obsidian_graph as config
            settings=core.ROOT/".obsidian/graph.json"; settings.parent.mkdir(exist_ok=True)
            settings.write_text(json.dumps({"search":"old","scale":2,"showOrphans":True}),encoding="utf-8")
            out=config.configure(core.ROOT,True)
            now=json.loads(settings.read_text(encoding="utf-8"))
            assert now["scale"]==2 and now["showOrphans"] is True and now["search"]==config.QUERY
            assert json.loads(Path(out["backup"]).read_text(encoding="utf-8"))["search"]=="old"
            assert not config.configure(core.ROOT,True)["changed"]
        """)


    def test_adversarial_empty_body_raw_coordinates_and_lost_id_restart(self):
        self.case("""
            a=node("A"); job=organization.plan("W1")
            write.update_node("A",body="",expect_hash=a["new_hash"])
            rejected(lambda: organization.review(job["key"],"W1","complete","No content retained",after=organization.snapshot("W1")["snapshot"]))
            p=write._live_locate(a["id"],graph.Index())
            write.update_node("A",body="Restored knowledge",expect_hash=core.sha256_file(p))
            rp=core.ROOT/"= Scope/W1/_raw/evidence.md"; rp.parent.mkdir(parents=True,exist_ok=True)
            rp.write_text("## 1"+chr(10)+"Observed",encoding="utf-8")
            for anchor in ("999","not-a-round"):
                ref="[[= Scope/W1/_raw/evidence.md#"+anchor+"]]"
                result=write.update_node("A",add_edges={"derived-from":ref})
                assert any(i["issue"]=="raw_round_unresolved" for i in result["reference_review"]["items"]),result
                rejected(lambda: organization.review(job["key"],"W1","complete","Invalid raw anchor",after=organization.snapshot("W1")["snapshot"]))
                write.update_node("A",remove_edges={"derived-from":ref})
            p.unlink()
            write.update_node("W1",old_text="- [[A]]",new_text="")
            resumed=organization.plan("W1")
            assert resumed["key"]==job["key"] and a["id"] in resumed["missing_ids"],resumed
            from osk import growth
            assert growth.plan(20)["organization_jobs"],"lost ID fell out of next run"
        """)

    def test_id_hub_navigation_matches_title_navigation(self):
        self.case("""
            s=node("Source")
            out=distillation.create_node({"key":"by-id","sources":["Source"],"hub":"W1"},title="Conclusion",summary="Conclusion",body="A bounded rule",drafter="gpt-6-astra",space="= Scope/W1")
            write.update_node("W1",old_text="[[Conclusion]]",new_text="[["+out["id"]+"]]")
            assert distillation.status("by-id")["status"]=="complete"
        """)

    def test_other_scope_damage_does_not_hide_raw_integration_prompt(self):
        self.case("""
            from osk import integration
            transcript=core.ROOT/"trial.jsonl"
            rows=[{"type":"user","sessionId":"trial","uuid":"u","message":{"role":"user","content":"Observed a durable rule"}},
                  {"type":"assistant","sessionId":"trial","uuid":"a","message":{"role":"assistant","id":"m","stop_reason":"end_turn","content":[{"type":"text","text":"A bounded conclusion"}]}}]
            transcript.write_text(chr(10).join(json.dumps(r) for r in rows),encoding="utf-8")
            st=integration.capture("claude","trial",str(transcript),"trial",space="= Scope/W1")
            assert st["pending_refs"],st
            broken=core.ROOT/"= Scope/W2/Broken.md"; broken.parent.mkdir(parents=True)
            broken.write_text("No frontmatter",encoding="utf-8")
            result=integration.prompt("claude","trial")
            assert st["pending_refs"][0] in result["text"],result
            assert "organization needs" in result["text"],result
        """)


if __name__=="__main__":
    unittest.main()

