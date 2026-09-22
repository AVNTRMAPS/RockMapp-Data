#!/usr/bin/env python3
import argparse, gzip, hashlib, json, os, pathlib, shutil, subprocess, urllib.request
from datetime import datetime, timezone

def load_json(path):
    if str(path).endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    if str(path).endswith(".gz"):
        raw=json.dumps(data, ensure_ascii=False, separators=(",",":")).encode("utf-8")
        with open(path, "wb") as out:
            with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as gz:
                gz.write(raw)
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")

def sha256(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()

def ensure_download(url, path):
    path=pathlib.Path(path)
    if path.exists():
        return
    req=urllib.request.Request(url, headers={"User-Agent":"GroundRove-QA"})
    with urllib.request.urlopen(req) as r, open(path,"wb") as out:
        shutil.copyfileobj(r,out)

def source_info(row, source_map):
    ids=(row.get("source_ids") or row.get("candidate_source_ids") or [])
    code=ids[0] if ids else "GROUNDROVE_VERIFIED_DELTA"
    info=source_map.get(code,{})
    title=info.get("title") or info.get("publisher") or code
    links=[source_map[x].get("url") for x in ids if x in source_map and source_map[x].get("url")]
    return code,title,links,ids

def append_unique(lst, value):
    if value and value not in lst:
        lst.append(value)

def add_provenance_note(record, row, source_map):
    code,title,links,ids=source_info(row,source_map)
    record.setdefault("source_links",[])
    for link in links:
        append_unique(record["source_links"],link)
    note=("GroundRove verified 2026-09-22 mineral/locality delta: "
          + row["canonical_material"] + " — " + row["locality_name"] + ".")
    if ids:
        note += " Supporting source IDs: " + ", ".join(ids) + "."
    if row.get("source_note"):
        note += " " + row["source_note"].strip()
    old=(record.get("source_note") or "").strip()
    if note not in old:
        record["source_note"]=(old+" "+note).strip()
    enrichments=record.setdefault("groundrove_verified_enrichments",[])
    entry={
        "source_row_id":row["id"],
        "material":row["canonical_material"],
        "locality":row["locality_name"],
        "source_ids":ids,
        "source_verification":row.get("source_verification",""),
        "location_precision":row.get("location_precision","")
    }
    if not any(x.get("source_row_id")==row["id"] for x in enrichments if isinstance(x,dict)):
        enrichments.append(entry)

def make_record(row, source_map):
    code,title,links,ids=source_info(row,source_map)
    is_area=row.get("delta_action")=="AREA_EVIDENCE"
    geom=row.get("geometry") if isinstance(row.get("geometry"),dict) else None
    is_point=bool(geom and geom.get("type")=="Point" and isinstance(geom.get("coordinates"),list)
                  and len(geom["coordinates"])>=2)
    spatial="area_evidence" if is_area else ("point_occurrence" if is_point else "unresolved_occurrence")
    rec={
        "id":row["id"],
        "name":row["locality_name"],
        "status":"",
        "grade":"",
        "materials":[row["canonical_material"]],
        "commodities":[],
        "alternate_names":[],
        "previous_names":[],
        "commodity_codes":[],
        "source_terms":[],
        "occurrence_descriptions":[],
        "context_terms":[],
        "source_links":links,
        "districts":[],
        "models":[],
        "rocks":[],
        "source_code":code,
        "evidence_type":"Area evidence" if is_area else "Documented mineral occurrence",
        "location_precision":row.get("location_precision",""),
        "source_title":title,
        "source_reliability":row.get("source_verification",""),
        "source_note":row.get("source_note",""),
        "point_eligible":bool(is_point),
        "heatmap_eligible":False,
        "spatial_type":spatial,
        "groundrove_source_row_id":row["id"],
        "groundrove_dedupe_key":row.get("dedupe_key",""),
        "groundrove_geometry_status":row.get("geometry_status","")
    }
    if row.get("area_scope_type"):
        rec["area_scope_type"]=row["area_scope_type"]
    if is_point:
        lon,lat=geom["coordinates"][:2]
        rec["lat"]=float(lat); rec["lon"]=float(lon)
    return rec

def records(root):
    arr=root.get("records")
    if not isinstance(arr,list):
        raise RuntimeError("index missing records array")
    return arr

def find_id(roots, rid):
    for root in roots:
        for rec in records(root):
            if str(rec.get("id",""))==str(rid):
                return rec
    return None

def normalize_root_counts(root):
    n=len(records(root))
    for key in ("recordCount","record_count","count"):
        if key in root and isinstance(root[key],int):
            root[key]=n
    root["groundrove_patch"]={
        "id":"20260922-full142",
        "date":"2026-09-22",
        "note":"Verified CO/AZ mineral/locality delta; spatial precision retained without invented geometry."
    }

def patch_state(state, prep, manifest_path, workdir, old_tag, new_tag):
    workdir=pathlib.Path(workdir)
    manifest=load_json(manifest_path)
    file_specs={x["id"]:x for x in manifest.get("files",[]) if isinstance(x,dict) and x.get("id")}
    for required in ("minerals","mineral_localities","mineral_evidence"):
        if required not in file_specs:
            raise RuntimeError(f"{state}: manifest missing {required}")
        spec=file_specs[required]
        ensure_download(spec["url"], workdir/spec["fileName"])

    mineral_path=workdir/file_specs["minerals"]["fileName"]
    locality_path=workdir/file_specs["mineral_localities"]["fileName"]
    evidence_path=workdir/file_specs["mineral_evidence"]["fileName"]
    mineral_root=load_json(mineral_path)
    locality_root=load_json(locality_path)
    evidence_root=load_json(evidence_path)
    roots_by_dataset={
        "minerals":[mineral_root],
        "mineral_evidence":[evidence_root],
        "mineral_localities":[locality_root]
    }
    source_map={x["id"]:x for x in prep.get("sources",[]) if isinstance(x,dict) and x.get("id")}
    rows=[r for r in prep["records"] if r.get("state")==state]
    applied={"ENRICH_EXISTING":0,"NEW_OCCURRENCE":0,"AREA_EVIDENCE":0}
    fallback_enrich=0

    existing_ids={str(x.get("id","")) for x in records(evidence_root)}
    for row in rows:
        action=row["delta_action"]
        if action=="ENRICH_EXISTING":
            target=row.get("target_record") or {}
            dataset=target.get("dataset","")
            rec=find_id(roots_by_dataset.get(dataset,[]),target.get("id"))
            if rec is None:
                # Preserve the verified relationship without pretending it was attached to an
                # unavailable base object. Use the exact already-resolved target geometry.
                rec=make_record(row,source_map)
                rec["id"]=row["id"]
                rec["name"]=target.get("name") or row["locality_name"]
                rec["lat"]=target["lat"]; rec["lon"]=target["lon"]
                rec["point_eligible"]=True
                rec["spatial_type"]="point_occurrence"
                rec["source_note"]=(rec.get("source_note","")+" Exact existing target was resolved in staging "
                                    f"to {dataset}:{target.get('id')}; this QA package carries the relationship "
                                    "as an evidence record because that base target was not present in the downloaded index.").strip()
                if rec["id"] not in existing_ids:
                    records(evidence_root).append(rec); existing_ids.add(rec["id"])
                fallback_enrich+=1
            else:
                rec.setdefault("materials",[])
                append_unique(rec["materials"],row["canonical_material"])
                add_provenance_note(rec,row,source_map)
            applied[action]+=1
        elif action in ("NEW_OCCURRENCE","AREA_EVIDENCE"):
            if row["id"] not in existing_ids:
                records(evidence_root).append(make_record(row,source_map))
                existing_ids.add(row["id"])
            applied[action]+=1
        else:
            raise RuntimeError(f"unexpected action {action}")

    for root in (mineral_root,locality_root,evidence_root):
        normalize_root_counts(root)
    save_json(mineral_path,mineral_root)
    save_json(locality_path,locality_root)
    save_json(evidence_path,evidence_root)

    # Point all three index specs at this QA release and refresh hashes/sizes.
    for fid in ("minerals","mineral_localities","mineral_evidence"):
        spec=file_specs[fid]
        p=workdir/spec["fileName"]
        spec["url"]=f"https://github.com/AVNTRMAPS/RockMapp-Data/releases/download/{new_tag}/{spec['fileName']}"
        spec["sha256"]=sha256(p)
        spec["bytes"]=p.stat().st_size
    manifest["version"]=str(manifest.get("version","qa"))+"-full142"
    manifest["publishedAt"]="2026-09-22T00:00:00Z"
    manifest["message"]="QA: September 22 all-qualified minerals plus verified 142-row CO/AZ mineral/locality delta."
    save_json(manifest_path,manifest)

    provenance={
        "schema":1,
        "status":"qa_full142",
        "state":state,
        "base_release":old_tag,
        "release":new_tag,
        "input_relationships":len(rows),
        "applied":applied,
        "fallback_exact_target_evidence_records":fallback_enrich,
        "spatial_policy":{
            "point":"Only source/target geometry already explicitly verified.",
            "area":"Area evidence retained as area evidence; no polygon invented where none was verified.",
            "unresolved":"Searchable evidence retained without a map point."
        }
    }
    save_json(workdir/"FULL142-PROVENANCE.json",provenance)

    # Validate all intended relationships are represented.
    assert sum(applied.values())==len(rows)
    byid={str(x.get("id","")):x for x in records(evidence_root)}
    for row in rows:
        if row["delta_action"]=="ENRICH_EXISTING":
            target=row["target_record"]
            rec=find_id(roots_by_dataset.get(target["dataset"],[]),target["id"])
            if rec is not None:
                assert row["canonical_material"] in rec.get("materials",[])
            else:
                assert row["id"] in byid
        else:
            assert row["id"] in byid
    return applied,fallback_enrich

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--state",required=True,choices=["CO","AZ"])
    ap.add_argument("--prep",required=True)
    ap.add_argument("--manifest",required=True)
    ap.add_argument("--workdir",required=True)
    ap.add_argument("--old-tag",required=True)
    ap.add_argument("--new-tag",required=True)
    args=ap.parse_args()
    prep=load_json(args.prep)
    if prep.get("summary",{}).get("total_delta_relationships")!=142:
        raise RuntimeError("unexpected prep count")
    applied,fallback=patch_state(args.state,prep,args.manifest,args.workdir,args.old_tag,args.new_tag)
    print(json.dumps({"state":args.state,"applied":applied,"fallback_enrich":fallback},sort_keys=True))

if __name__=="__main__":
    main()
