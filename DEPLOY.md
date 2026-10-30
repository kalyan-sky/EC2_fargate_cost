# Deploying the demo on AWS EKS Fargate

There are two ways to run it:

- **A. New throwaway cluster.** Safest. Takes about 20 minutes, and you delete it all at the end.
- **B. Your existing cluster.** Adds one Fargate profile for a separate `fargate-cost-demo` namespace.
  Nothing in `ns-prdizcps` is touched.

Commands below use `ap-southeast-1`. Change `REGION` if yours is different.

## 0. Prerequisites (once)

Install the tools (Linux x86_64; on macOS use `brew install awscli eksctl kubectl`):

```bash
# AWS CLI v2
curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip -q awscliv2.zip && sudo ./aws/install

# eksctl
curl -sSL "https://github.com/eksctl-io/eksctl/releases/latest/download/eksctl_Linux_amd64.tar.gz" | tar xz -C /tmp
sudo mv /tmp/eksctl /usr/local/bin/

# kubectl
curl -sSLO "https://dl.k8s.io/release/$(curl -sSL https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -m 0755 kubectl /usr/local/bin/kubectl
```

Log in and check:

```bash
aws configure                    # or: aws sso login --profile <profile>; export AWS_PROFILE=<profile>
aws sts get-caller-identity      # must print your account/role
eksctl version && kubectl version --client && python3 --version
```

Your IAM identity needs permission to create EKS, EC2/VPC, IAM roles and CloudFormation stacks
(AdministratorAccess on a sandbox account is simplest).

Get the code:

```bash
git clone https://github.com/kalyan-sky/EC2_fargate_cost.git
cd EC2_fargate_cost
git checkout claude/eager-davinci-rjh3cq
export REGION=ap-southeast-1
```

## 1A. Create a new Fargate-only cluster

```bash
# edit region in demo/cluster.yaml first if not ap-southeast-1
eksctl create cluster -f demo/cluster.yaml        # ~15-20 min
```

This creates the VPC, the EKS control plane, the Fargate pod execution role and two Fargate profiles
(`default`/`kube-system`, and `fargate-cost-demo`). It also writes your `~/.kube/config`.

Check it:

```bash
kubectl get pods -n kube-system                   # coredns pods Running
kubectl get nodes                                 # fargate-ip-... nodes (one per pod)
eksctl get fargateprofile --cluster fargate-cost-demo --region $REGION
```

Then go to step 2 and use `CLUSTER=fargate-cost-demo`.

## 1B. Or use your existing cluster

```bash
export CLUSTER=<your-cluster-name>
aws eks update-kubeconfig --name $CLUSTER --region $REGION
kubectl config current-context                    # make sure it's the right cluster

eksctl create fargateprofile --cluster $CLUSTER --region $REGION \
  --name fp-cost-demo --namespace fargate-cost-demo
eksctl get fargateprofile --cluster $CLUSTER --region $REGION
```

The profile **must exist before** you apply the manifest. Otherwise the pods stay `Pending` because
no node can run them. Fargate pods run in the cluster's private subnets and pull `polinux/stress` from
Docker Hub, so those subnets need outbound internet (a NAT gateway).

## 2. Deploy the 3 services and watch the OOMKill

```bash
kubectl apply -f demo/oom-demo.yaml
kubectl get pods -n fargate-cost-demo -w          # ~1 min per pod to start on Fargate; Ctrl+C to stop
```

Expected: `account-service` and `notification-service` are `Running`. `transaction-service` cycles
through `OOMKilled` → `CrashLoopBackOff` with a rising RESTARTS count.

```bash
kubectl describe pod -n fargate-cost-demo -l app=transaction-service \
  | grep -E 'CapacityProvisioned|Last State|Reason|Restart Count'
#   CapacityProvisioned: 0.25vCPU 0.5GB
#   Last State: Terminated   Reason: OOMKilled

kubectl get pods -n fargate-cost-demo \
  -o custom-columns=POD:.metadata.name,SIZE:.metadata.annotations.CapacityProvisioned,STATUS:.status.phase
```

## 3. BEFORE snapshot

```bash
python3 fargate_cost.py --region $REGION snapshot -n fargate-cost-demo -o before.json
```

The snapshot only counts pods in phase `Running`. A crash-looping pod is still `Running`, so it is
included.

## 4. Apply the fix (same command as production)

```bash
kubectl set resources deployment/transaction-service -c sla-cps-transaction-service \
  --requests=cpu=500m,memory=1536Mi --limits=memory=1536Mi -n fargate-cost-demo

kubectl rollout status deployment/transaction-service -n fargate-cost-demo
kubectl get pods -n fargate-cost-demo -w          # new pod Running, RESTARTS 0
```

Fargate can't resize a running pod. The new ReplicaSet gets a new, bigger Fargate VM, and the old one
is terminated:

```bash
kubectl describe pod -n fargate-cost-demo -l app=transaction-service | grep CapacityProvisioned
#   CapacityProvisioned: 0.5vCPU 2GB
```

## 5. AFTER snapshot and compare

```bash
python3 fargate_cost.py --region $REGION snapshot -n fargate-cost-demo -o after.json
python3 fargate_cost.py --region $REGION compare before.json after.json
```

You should see transaction-service go from `0.25vCPU 0.5GB` ($11.25/mo) to `0.5vCPU 2GB`
($26.53/mo), with the other two unchanged.

Optional, the cheaper fix (memory only):

```bash
kubectl set resources deployment/transaction-service -c sla-cps-transaction-service \
  --requests=cpu=250m,memory=1536Mi --limits=memory=1536Mi -n fargate-cost-demo
# -> 0.25vCPU 2GB, $17.30/mo
```

## 6. Clean up (do not skip — you pay while it exists)

If you used a new cluster (1A), delete everything:

```bash
kubectl delete namespace fargate-cost-demo
eksctl delete cluster -f demo/cluster.yaml --wait # ~10 min; also removes VPC and NAT gateway
```

If you used your existing cluster (1B), remove only the demo:

```bash
kubectl delete namespace fargate-cost-demo
eksctl delete fargateprofile --cluster $CLUSTER --region $REGION --name fp-cost-demo
```

## What the demo costs while running (1A, ap-southeast-1, approx.)

| Item | $/hour |
|---|---|
| EKS control plane | $0.10 |
| NAT gateway (+ data) | ~$0.06 |
| 2 CoreDNS pods (0.25vCPU 0.5GB each) | ~$0.03 |
| 3 demo pods | ~$0.05–0.07 |
| **Total** | **~$0.25/hour** |

An hour-long run costs well under $1.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Pods `Pending`, event *"no nodes available"* | No Fargate profile matches the namespace. Check `eksctl get fargateprofile`; create it, then `kubectl delete pod` to reschedule |
| `ErrImagePull` / `ImagePullBackOff` | Private subnets have no internet. Add a NAT gateway, or push the image to ECR |
| CoreDNS `Pending` on a new cluster | No profile for `kube-system`. Use `demo/cluster.yaml` as is |
| `kubectl` says *Unauthorized* | Wrong context or IAM identity. Run `aws sts get-caller-identity` and `aws eks update-kubeconfig ...` |
| `CapacityProvisioned` missing | The pod is on an EC2 node, not Fargate. Check the namespace/labels match the profile |
