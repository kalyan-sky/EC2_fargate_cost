# EC2_fargate_cost

Did my EKS Fargate bill change after I fixed an OOMKilled pod by raising its resources?
This repo answers that, and lets you replay the whole exercise on a test namespace.

## How Fargate charges for a pod

Fargate does **not** bill for what the pod uses. It bills for the VM size it provisions, which
it works out from the pod's **requests**:

1. pod request = sum of app-container requests (or the largest init container, whichever is bigger)
2. **+256 MiB** for kubelet / kube-proxy / containerd
3. rounded **up** to the next valid vCPU/GB combination
4. the result is written on the pod as the `CapacityProvisioned` annotation

| vCPU | Memory options |
|------|----------------|
| 0.25 | 0.5, 1, 2 GB |
| 0.5  | 1 – 4 GB |
| 1    | 2 – 8 GB |
| 2    | 4 – 16 GB |
| 4    | 8 – 30 GB |

Billing: per second (1 minute minimum), `vCPU × $/vCPU-h + GB × $/GB-h`.

## The real change (transaction-service)

```
before:  CapacityProvisioned: 0.25vCPU 0.5GB
change:  kubectl set resources deployment/transaction-service -c sla-cps-transaction-service \
           --requests=cpu=500m,memory=1536Mi --limits=memory=1536Mi -n ns-prdizcps
after:   1536Mi + 256Mi = 1792Mi = 1.75 GB  -> rounds to 0.5vCPU 2GB
```

At ap-southeast-1 Linux/x86 prices ($0.05056/vCPU-h, $0.00553/GB-h; check the
[pricing page](https://aws.amazon.com/fargate/pricing/) for your region):

| | Size | $/hour | $/month (730 h) |
|---|---|---|---|
| Before, per pod | 0.25vCPU 0.5GB | $0.0154 | $11.25 |
| After, per pod  | 0.5vCPU 2GB    | $0.0363 | $26.53 |
| **Change, per pod** | | +$0.0209 | **+$15.28 (≈2.4×)** |

Only `transaction-service` changes. The other two services stay the same unless you resize them too.
If transaction-service runs N replicas, multiply the change by N.

## Check your live cluster

Requires `kubectl` (already pointed at the cluster) and Python 3. No other packages needed.

```bash
# What size/cost would a given request get?
python3 fargate_cost.py predict --cpu 500m --memory 1536Mi --replicas 3

# What are the 3 services provisioned at right now, and what do they cost?
python3 fargate_cost.py snapshot -n ns-prdizcps \
  -d transaction-service -d <service-2> -d <service-3> -o after.json

# Quick look without the script:
kubectl get pods -n ns-prdizcps \
  -o custom-columns=POD:.metadata.name,SIZE:.metadata.annotations.CapacityProvisioned
```

To compare against the old size when you have no "before" snapshot, compute a snapshot from a
saved `kubectl get pods -o json` taken earlier (`--from-file`), or use `predict` with the old
requests (no requests at all = 0.25vCPU 0.5GB).

Other regions: `--region us-east-1`, or set the prices yourself with `--vcpu-price X --gb-price Y`.

## Replicate the exercise end to end

Full step-by-step instructions, including creating the EKS Fargate cluster: [DEPLOY.md](DEPLOY.md).

`demo/oom-demo.yaml` creates three small services in namespace `fargate-cost-demo`. They start at
0.25vCPU 0.5GB, and `transaction-service` needs ~700 MiB against a 400Mi limit, so it gets OOMKilled.

```bash
# 0. Fargate profile for the demo namespace (once)
eksctl create fargateprofile --cluster <cluster> --name cost-demo --namespace fargate-cost-demo

# 1. Deploy and watch transaction-service hit OOMKilled / CrashLoopBackOff
kubectl apply -f demo/oom-demo.yaml
kubectl get pods -n fargate-cost-demo -w
kubectl describe pod -n fargate-cost-demo -l app=transaction-service | grep -E 'CapacityProvisioned|Reason|Restart'

# 2. Take the BEFORE snapshot
python3 fargate_cost.py snapshot -n fargate-cost-demo -o before.json

# 3. Apply the same fix as production (creates a new rollout; Fargate starts a new, bigger VM)
kubectl set resources deployment/transaction-service -c sla-cps-transaction-service \
  --requests=cpu=500m,memory=1536Mi --limits=memory=1536Mi -n fargate-cost-demo
kubectl rollout status deployment/transaction-service -n fargate-cost-demo

# 4. Take the AFTER snapshot and compare
python3 fargate_cost.py snapshot -n fargate-cost-demo -o after.json
python3 fargate_cost.py compare before.json after.json

# 5. Clean up (Fargate bills until the pods are gone)
kubectl delete namespace fargate-cost-demo
eksctl delete fargateprofile --cluster <cluster> --name cost-demo
```

Expected `compare` output:

```
DEPLOYMENT              BEFORE              AFTER               BEFORE $/MO  AFTER $/MO  DELTA $/MO
account-service         1x 0.25vCPU 0.5GB   1x 0.25vCPU 0.5GB        $11.25      $11.25    +$0.0000
notification-service    1x 0.25vCPU 0.5GB   1x 0.25vCPU 0.5GB        $11.25      $11.25    +$0.0000
transaction-service     1x 0.25vCPU 0.5GB   1x 0.5vCPU 2GB           $11.25      $26.53     +$15.28
TOTAL                                                                $33.74      $49.02     +$15.28
```

## Confirm on the bill

In Cost Explorer, filter **Service = EC2 Container Service** (Fargate on EKS is billed there) and
group by **Usage type**. Look at `<REGION>-Fargate-vCPU-Hours:perCPU` and `<REGION>-Fargate-GB-Hours`
(e.g. `APS1-...` for Singapore), daily, around the change date. The day after the change, GB-hours
should go up by about `(2 − 0.5) × 24 = 36` per transaction-service replica, and vCPU-hours by about
`(0.5 − 0.25) × 24 = 6`.

## Tips

- Memory is billed in whole-GB steps after the 256Mi overhead is added. Any request from 769Mi to
  1792Mi lands on 2GB and costs the same, so you could raise the request to 1792Mi for free. At
  1793Mi it jumps to 3GB.
- CPU is the more expensive part. 500m doubles the vCPU charge. If the OOM was only about memory,
  `--requests=cpu=250m,memory=1536Mi` gives **0.25vCPU 2GB** ($0.0237/h, $17.30/month) instead
  of 0.5vCPU 2GB.
