#!/usr/bin/env python3
"""EKS Fargate pod cost calculator (stdlib only).

Sub-commands:
  predict   Show what Fargate will provision (and charge) for given requests.
  snapshot  Read live pods via kubectl and save/print their provisioned size + cost.
  compare   Diff two snapshots (before vs after a resource change).

How EKS Fargate sizes a pod (AWS docs, "Fargate pod configuration"):
  * pod request = max(largest init-container request, sum of app-container requests)
  * +256 MiB memory for kubelet / kube-proxy / containerd
  * rounded UP to the nearest valid vCPU/memory combination below
  * no requests at all -> 0.25 vCPU / 0.5 GB
  * the result is written on the pod as annotation "CapacityProvisioned"
Billing is per second (1 minute minimum) on the provisioned vCPU and GB.
"""
import argparse
import json
import math
import re
import subprocess
import sys
from datetime import datetime, timezone

HOURS_PER_MONTH = 730

# Linux/x86 Fargate on-demand prices (USD per vCPU-hour, per GB-hour).
# Verify for your region at https://aws.amazon.com/fargate/pricing/ and
# override with --vcpu-price / --gb-price if they differ.
PRICES = {
    "us-east-1": (0.04048, 0.004445),
    "us-west-2": (0.04048, 0.004445),
    "eu-west-1": (0.04048, 0.004445),
    "ap-southeast-1": (0.05056, 0.00553),
    "ap-east-1": (0.05715, 0.00625),
    "ap-northeast-1": (0.05056, 0.00553),
    "ap-south-1": (0.04256, 0.00467),
}

# Valid Fargate (vCPU, memory GB options).
CONFIGS = [
    (0.25, [0.5, 1, 2]),
    (0.5, [1, 2, 3, 4]),
    (1, list(range(2, 9))),
    (2, list(range(4, 17))),
    (4, list(range(8, 31))),
    (8, list(range(16, 61, 4))),
    (16, list(range(32, 121, 8))),
]

FARGATE_OVERHEAD_MIB = 256


def parse_cpu(q):
    """'500m' -> 0.5, '1' -> 1.0"""
    if q is None:
        return 0.0
    q = str(q)
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


_MEM_UNITS = {
    "Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 1024 ** 2,
    "K": 1000 / 1024 ** 2, "M": 1000 ** 2 / 1024 ** 2,
    "G": 1000 ** 3 / 1024 ** 2, "T": 1000 ** 4 / 1024 ** 2,
    "": 1 / 1024 ** 2,  # plain bytes
}


def parse_mem_mib(q):
    """'1536Mi' -> 1536, '2Gi' -> 2048, '512M' -> 488.3"""
    if q is None:
        return 0.0
    m = re.fullmatch(r"([0-9.]+)([KMGT]i?)?", str(q))
    if not m:
        raise ValueError(f"cannot parse memory quantity {q!r}")
    return float(m.group(1)) * _MEM_UNITS[m.group(2) or ""]


def fargate_size(cpu, mem_mib):
    """Round a pod request (vCPU, MiB, overhead NOT included) to a Fargate size."""
    if cpu == 0 and mem_mib == 0:
        return 0.25, 0.5
    need_gb = (mem_mib + FARGATE_OVERHEAD_MIB) / 1024
    for vcpu, mems in CONFIGS:
        if vcpu < cpu:
            continue
        for gb in mems:
            if gb >= need_gb:
                return vcpu, gb
    raise ValueError(f"request {cpu} vCPU / {mem_mib} MiB exceeds largest Fargate size")


def hourly(vcpu, gb, prices):
    return vcpu * prices[0] + gb * prices[1]


def pod_requests(spec):
    """Effective pod request per Fargate rules (requests, falling back to limits)."""
    def res(c):
        r = c.get("resources", {})
        req, lim = r.get("requests", {}), r.get("limits", {})
        return (parse_cpu(req.get("cpu", lim.get("cpu"))),
                parse_mem_mib(req.get("memory", lim.get("memory"))))

    app = [res(c) for c in spec.get("containers", [])]
    init = [res(c) for c in spec.get("initContainers", [])]
    cpu = max(sum(c for c, _ in app), max((c for c, _ in init), default=0))
    mem = max(sum(m for _, m in app), max((m for _, m in init), default=0))
    return cpu, mem


def parse_capacity(annotation):
    """'0.25vCPU 0.5GB' -> (0.25, 0.5)"""
    m = re.match(r"\s*([0-9.]+)vCPU\s+([0-9.]+)GB", annotation or "")
    return (float(m.group(1)), float(m.group(2))) if m else None


def owner_name(pod):
    """Deployment name from the ReplicaSet owner (strip pod-template-hash)."""
    for ref in pod["metadata"].get("ownerReferences", []):
        if ref["kind"] == "ReplicaSet":
            h = pod["metadata"].get("labels", {}).get("pod-template-hash")
            return ref["name"][: -len(h) - 1] if h else ref["name"]
        return ref["name"]
    return pod["metadata"]["name"]


def get_prices(args):
    base = PRICES.get(args.region)
    if base is None and (args.vcpu_price is None or args.gb_price is None):
        sys.exit(f"no built-in price for {args.region}; pass --vcpu-price and --gb-price")
    base = base or (0, 0)
    return (args.vcpu_price or base[0], args.gb_price or base[1])


def money(x):
    return f"${x:,.4f}" if x < 1 else f"${x:,.2f}"


# ---------------------------------------------------------------- commands

def cmd_predict(args):
    prices = get_prices(args)
    cpu, mem = parse_cpu(args.cpu), parse_mem_mib(args.memory)
    vcpu, gb = fargate_size(cpu, mem)
    h = hourly(vcpu, gb, prices)
    print(f"Requested     : {cpu} vCPU, {mem:.0f} MiB (+{FARGATE_OVERHEAD_MIB} MiB Fargate overhead)")
    print(f"Provisioned   : {vcpu:g}vCPU {gb:g}GB")
    print(f"Per pod       : {money(h)}/hour  {money(h * HOURS_PER_MONTH)}/month")
    print(f"x{args.replicas} replicas".ljust(14) + ": " + f"{money(h * args.replicas)}/hour  "
          f"{money(h * args.replicas * HOURS_PER_MONTH)}/month")


def cmd_snapshot(args):
    prices = get_prices(args)
    if args.from_file:
        with open(args.from_file) as f:
            data = json.load(f)
    else:
        cmd = ["kubectl", "get", "pods", "-n", args.namespace, "-o", "json"]
        if args.selector:
            cmd += ["-l", args.selector]
        data = json.loads(subprocess.check_output(cmd))

    pods = []
    for p in data["items"]:
        if p["status"].get("phase") != "Running":
            continue
        dep = owner_name(p)
        if args.deployment and dep not in args.deployment:
            continue
        cap = parse_capacity(p["metadata"].get("annotations", {}).get("CapacityProvisioned"))
        cpu, mem = pod_requests(p["spec"])
        predicted = fargate_size(cpu, mem)
        vcpu, gb = cap or predicted
        restarts = sum(c.get("restartCount", 0) for c in p["status"].get("containerStatuses", []))
        pods.append({
            "deployment": dep,
            "pod": p["metadata"]["name"],
            "provisioned": f"{vcpu:g}vCPU {gb:g}GB",
            "source": "annotation" if cap else "predicted",
            "vcpu": vcpu, "gb": gb,
            "request_cpu": cpu, "request_mem_mib": round(mem),
            "restarts": restarts,
            "hourly": hourly(vcpu, gb, prices),
        })

    snap = {
        "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "namespace": args.namespace, "region": args.region,
        "prices": {"vcpu_hour": prices[0], "gb_hour": prices[1]},
        "pods": pods,
    }
    print_snapshot(snap)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(snap, f, indent=2)
        print(f"\nSaved snapshot to {args.out}")


def by_deployment(snap):
    agg = {}
    for p in snap["pods"]:
        a = agg.setdefault(p["deployment"], {"pods": 0, "hourly": 0.0, "sizes": set()})
        a["pods"] += 1
        a["hourly"] += p["hourly"]
        a["sizes"].add(p["provisioned"])
    return agg


def print_snapshot(snap):
    print(f"Snapshot {snap['taken_at']}  ns={snap['namespace']}  region={snap['region']}  "
          f"(${snap['prices']['vcpu_hour']}/vCPU-h, ${snap['prices']['gb_hour']}/GB-h)\n")
    fmt = "{:<45} {:<16} {:>12} {:>9} {:>10}"
    print(fmt.format("POD", "PROVISIONED", "REQUEST", "RESTARTS", "$/MONTH"))
    for p in snap["pods"]:
        req = f"{p['request_cpu']}/{p['request_mem_mib']}Mi"
        mark = "" if p["source"] == "annotation" else "*"
        print(fmt.format(p["pod"][:45], p["provisioned"] + mark, req, p["restarts"],
                         money(p["hourly"] * HOURS_PER_MONTH)))
    print()
    total = 0.0
    for dep, a in by_deployment(snap).items():
        total += a["hourly"]
        print(f"  {dep:<40} {a['pods']} pod(s)  {', '.join(sorted(a['sizes'])):<20} "
              f"{money(a['hourly'] * HOURS_PER_MONTH)}/month")
    print(f"  {'TOTAL':<40} {money(total)}/hour  {money(total * HOURS_PER_MONTH)}/month")
    if any(p["source"] != "annotation" for p in snap["pods"]):
        print("\n  * no CapacityProvisioned annotation (not on Fargate?) - size predicted from requests")


def cmd_compare(args):
    with open(args.before) as f:
        before = json.load(f)
    with open(args.after) as f:
        after = json.load(f)
    b, a = by_deployment(before), by_deployment(after)

    fmt = "{:<35} {:<22} {:<22} {:>12} {:>12} {:>12}"
    print(fmt.format("DEPLOYMENT", "BEFORE", "AFTER", "BEFORE $/MO", "AFTER $/MO", "DELTA $/MO"))
    tb = ta = 0.0
    for dep in sorted(set(b) | set(a)):
        x = b.get(dep, {"pods": 0, "hourly": 0.0, "sizes": set()})
        y = a.get(dep, {"pods": 0, "hourly": 0.0, "sizes": set()})
        tb += x["hourly"]
        ta += y["hourly"]
        bm, am = x["hourly"] * HOURS_PER_MONTH, y["hourly"] * HOURS_PER_MONTH
        print(fmt.format(dep[:35],
                         f"{x['pods']}x {'/'.join(sorted(x['sizes'])) or '-'}",
                         f"{y['pods']}x {'/'.join(sorted(y['sizes'])) or '-'}",
                         money(bm), money(am), ("+" if am >= bm else "") + money(am - bm)))
    bm, am = tb * HOURS_PER_MONTH, ta * HOURS_PER_MONTH
    pct = f"{(am - bm) / bm * 100:+.1f}%" if bm else "n/a"
    print(fmt.format("TOTAL", "", "", money(bm), money(am), ("+" if am >= bm else "") + money(am - bm)))
    print(f"\nChange: {pct}  ({money(tb)}/h -> {money(ta)}/h)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="ap-southeast-1")
    ap.add_argument("--vcpu-price", type=float, help="override USD per vCPU-hour")
    ap.add_argument("--gb-price", type=float, help="override USD per GB-hour")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("predict", help="size + cost for a given request")
    p.add_argument("--cpu", default="0", help="e.g. 500m")
    p.add_argument("--memory", default="0", help="e.g. 1536Mi")
    p.add_argument("--replicas", type=int, default=1)
    p.set_defaults(func=cmd_predict)

    s = sub.add_parser("snapshot", help="read live pods and compute cost")
    s.add_argument("-n", "--namespace", default="default")
    s.add_argument("-l", "--selector", help="label selector")
    s.add_argument("-d", "--deployment", action="append", help="only these deployments (repeatable)")
    s.add_argument("-o", "--out", help="write snapshot JSON here")
    s.add_argument("--from-file", help="use saved 'kubectl get pods -o json' output instead of kubectl")
    s.set_defaults(func=cmd_snapshot)

    c = sub.add_parser("compare", help="diff two snapshots")
    c.add_argument("before")
    c.add_argument("after")
    c.set_defaults(func=cmd_compare)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
