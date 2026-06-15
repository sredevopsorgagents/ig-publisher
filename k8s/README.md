Quick commands to create the Secret and ConfigMap for the `ig-publisher` Deployment

Create the Secret from a local file (recommended):

```bash
kubectl create secret generic gcp-sa-key \
  --from-file=gcp-sa-key.json=./gcp-sa-key.json \
  -n marketing-automation
```

Or apply the manifest in this directory after replacing the placeholder value:

```bash
kubectl apply -f k8s/gcp-sa-secret.yaml
kubectl apply -f k8s/ig-configmap.yaml
kubectl apply -f deployment.yaml
```

Create the ConfigMap with a literal value:

```bash
kubectl create configmap ig-config \
  --from-literal=gcs_bucket_name=your-bucket \
  -n marketing-automation
```
