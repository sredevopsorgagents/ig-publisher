# IG Publisher - Traefik v3 Ingress Configuration

This repository contains the Kubernetes configuration for deploying an Instagram Publisher service with Traefik v3 ingress and authentication middleware.

## Overview

The IG Publisher is a FastAPI-based web application that allows users to publish images and videos to Instagram via the Meta Graph API. This configuration provides:

- **Traefik v3 IngressRoute** for routing external traffic
- **Basic Authentication Middleware** for securing access
- **TLS/HTTPS support** for encrypted communication
- **Kubernetes Service** for internal cluster routing

## Prerequisites

Before deploying, ensure you have:

1. **Kubernetes cluster** (v1.25+)
2. **Traefik v3** installed in your cluster
3. **kubectl** configured to access your cluster
4. **htpasswd** utility (for generating password hashes)
5. **cert-manager** (optional, for automatic TLS certificate management)

## Files Included

```
├── traefik-ingress.yaml    # Traefik IngressRoute, Middleware, Service, and Secret
├── Dockerfile              # Container image definition
├── main.py                 # FastAPI application
├── batch.yaml              # Kubernetes Job for batch processing
└── README.md               # This file
```

## Deployment Instructions

### 1. Create Namespace

```bash
kubectl create namespace marketing-automation
```

### 2. Generate Basic Auth Credentials

Create a secure password hash for basic authentication:

```bash
# Install apache2-utils if htpasswd is not available
# Ubuntu/Debian: apt-get install apache2-utils
# macOS: brew install httpd

# Generate hash for username 'admin' with your chosen password
htpasswd -nb admin <your-secure-password> | base64
```

Update the `basic-auth-secret` in `traefik-ingress.yaml` with your generated hash:

```yaml
data:
  users: <your-base64-encoded-hash>
```

### 3. Create TLS Secret

You need a TLS certificate for HTTPS. Choose one of the following options:

#### Option A: Using cert-manager (Recommended)

Create a Certificate resource:

```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: ig-publisher-tls
  namespace: marketing-automation
spec:
  secretName: ig-publisher-tls-secret
  issuerRef:
    name: letsencrypt-prod
    kind: ClusterIssuer
  dnsNames:
    - ig-publisher.example.com
```

#### Option B: Manual TLS Secret

Generate a self-signed certificate (for development only):

```bash
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout tls.key -out tls.crt \
  -subj "/CN=ig-publisher.example.com"

kubectl create secret tls ig-publisher-tls-secret \
  --cert=tls.crt --key=tls.key \
  -n marketing-automation
```

### 4. Deploy the Application

Apply all configurations:

```bash
kubectl apply -f traefik-ingress.yaml
```

If you're using cert-manager, apply the Certificate resource first:

```bash
kubectl apply -f certificate.yaml
kubectl apply -f traefik-ingress.yaml
```

### 5. Verify Deployment

Check that all resources are created:

```bash
# Check IngressRoute
kubectl get ingressroute ig-publisher-ingress -n marketing-automation

# Check Middleware
kubectl get middleware basic-auth -n marketing-automation

# Check Service
kubectl get service ig-publisher-service -n marketing-automation

# Check Secret
kubectl get secret basic-auth-secret -n marketing-automation
```

### 6. Update DNS

Point your domain (`ig-publisher.example.com`) to your Traefik load balancer IP address.

## Configuration Reference

### IngressRoute

| Field | Value | Description |
|-------|-------|-------------|
| Host | `ig-publisher.example.com` | Domain name for accessing the service |
| EntryPoints | `websecure` | Traefik entrypoint for HTTPS (port 443) |
| Service Port | `8000` | Target port of the IG Publisher application |
| TLS Secret | `ig-publisher-tls-secret` | Kubernetes TLS secret name |

### Middleware

| Field | Value | Description |
|-------|-------|-------------|
| Type | `basicAuth` | HTTP Basic Authentication |
| Secret | `basic-auth-secret` | Kubernetes secret containing credentials |
| removeHeader | `true` | Remove Authorization header after authentication |

### Service

| Field | Value | Description |
|-------|-------|-------------|
| Type | `ClusterIP` | Internal cluster service |
| Port | `8000` | Service port |
| TargetPort | `8000` | Container port |

## Environment Variables

The application requires the following environment variables (configure in your Deployment):

| Variable | Description | Source |
|----------|-------------|--------|
| `IG_USER_ID` | Instagram User ID | Secret: `ig-credentials.user_id` |
| `IG_ACCESS_TOKEN` | Instagram Access Token | Secret: `ig-credentials.access_token` |
| `GCS_BUCKET_NAME` | Google Cloud Storage bucket | Secret or ConfigMap |
| `GCP_SA_KEY_PATH` | Path to GCP service account key | Secret or volume mount |

## Security Considerations

1. **Change Default Credentials**: Never use the default `admin/changeme` credentials in production.

2. **Use Strong Passwords**: Generate secure password hashes with strong passwords.

3. **Enable TLS**: Always use HTTPS in production environments.

4. **Restrict Access**: Consider adding IP whitelisting or additional authentication layers.

5. **Secret Management**: Use external secret management solutions (e.g., HashiCorp Vault, AWS Secrets Manager) for sensitive credentials.

## Troubleshooting

### Cannot Access the Service

1. Verify Traefik is running:
   ```bash
   kubectl get pods -n traefik-system
   ```

2. Check IngressRoute status:
   ```bash
   kubectl describe ingressroute ig-publisher-ingress -n marketing-automation
   ```

3. Verify TLS secret exists:
   ```bash
   kubectl get secret ig-publisher-tls-secret -n marketing-automation
   ```

### Authentication Not Working

1. Verify the secret format:
   ```bash
   kubectl get secret basic-auth-secret -n marketing-automation -o jsonpath='{.data.users}' | base64 -d
   ```

2. Test with curl:
   ```bash
   curl -u admin:password https://ig-publisher.example.com/
   ```

### TLS Certificate Issues

1. If using cert-manager, check certificate status:
   ```bash
   kubectl get certificate ig-publisher-tls -n marketing-automation
   kubectl describe certificate ig-publisher-tls -n marketing-automation
   ```

## Customization

### Change Domain

Update the `match` field in the IngressRoute:

```yaml
routes:
  - match: Host(`your-domain.com`)
```

### Add Multiple Domains

```yaml
routes:
  - match: Host(`ig-publisher.example.com`)
    kind: Rule
    services:
      - name: ig-publisher-service
        port: 8000
    middlewares:
      - name: basic-auth
  - match: Host(`publisher.internal.example.com`)
    kind: Rule
    services:
      - name: ig-publisher-service
        port: 8000
    middlewares:
      - name: basic-auth
```

### Add Rate Limiting

Create an additional Middleware:

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: rate-limit
  namespace: marketing-automation
spec:
  rateLimit:
    average: 100
    burst: 50
```

Then add it to the IngressRoute middlewares list.

## Cleanup

To remove all resources:

```bash
kubectl delete -f traefik-ingress.yaml
```

## Additional Resources

- [Traefik Documentation](https://doc.traefik.io/traefik/)
- [Traefik IngressRoute CRD](https://doc.traefik.io/traefik/routing/providers/kubernetes-crd/#kind-ingressroute)
- [Traefik Middleware](https://doc.traefik.io/traefik/middlewares/http/overview/)
- [Kubernetes Secrets](https://kubernetes.io/docs/concepts/configuration/secret/)

## License

This configuration is provided as-is for educational and production use.
