# 7. Deployment <!-- omit in toc -->

- [Prerequisites](#prerequisites)
- [1. Provision the infrastructure](#1-provision-the-infrastructure)
- [2. Deploy the proxy code](#2-deploy-the-proxy-code)
- [3. Custom domain](#3-custom-domain)
- [4. Verify](#4-verify)
- [Running the container yourself](#running-the-container-yourself)
- [Rolling out to a team](#rolling-out-to-a-team)
- [Upgrading](#upgrading)
- [Optional: publish the API specification](#optional-publish-the-api-specification)

## Prerequisites

- **Terraform** `>= 1.15.0` (providers `google` and `google-beta` pinned to `8.5.0`).
- **Google Cloud SDK** (`gcloud`) authenticated as an administrator who can create projects in the target folder and link a billing account.
- **Python 3.14** and **Docker** if you want to run or build the service locally.
- A Google Cloud **folder** and **billing account** for the hub project.
- A domain you own, with access to its DNS zone, and a Google account that can verify it in [Search Console](https://search.google.com/search-console) (see [Custom domain](#3-custom-domain)).

## 1. Provision the infrastructure

Verify ownership of `custom_domain` first ([Custom domain](#3-custom-domain)): the apply creates the domain mapping and Cloud Run refuses it for an unverified domain.

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # then edit every placeholder
terraform init
terraform plan
terraform apply
```

Terraform creates, in the hub project:

| Area | Resources |
| --- | --- |
| Project and APIs | Hub project, enabled APIs (Vertex AI, Cloud Run, Cloud Build, Artifact Registry, Logging, Monitoring, Trace, BigQuery, Billing Budgets, IAM, Storage) |
| Identity | Proxy service account with `aiplatform.user`, `logging.logWriter`, `cloudtrace.agent` |
| Proxy | Cloud Run service (CPU 1, 512 MiB, scaling 0 to 20, concurrency 8, timeout 300 s), invoker bindings, custom domain mapping |
| Analytics | BigQuery dataset, usage log table, Cloud Logging sink, normalized reporting view |
| Operations | Billing budget, notification channels, log-based metrics, dashboard, alert policies, Cloud Trace storage |

> **State.** Terraform state and `terraform.tfvars` contain project identifiers and identities. Do not commit them; configure a remote backend (for example a Cloud Storage bucket) for shared use.

Terraform creates the Cloud Run service with a placeholder image and then ignores image and build metadata, so the application is deployed separately (next step) without Terraform reverting it.

## 2. Deploy the proxy code

The script runs Cloud Build on the `api/` directory (using its `Dockerfile`) and rolls out a new revision. IAM, environment variables, scaling and the domain stay under Terraform's control.

```bash
# Linux / macOS
PROJECT_ID=my-ai-companion-hub REGION=europe-west1 ./api/deploy.sh
```

```powershell
# Windows
.\api\deploy.ps1 -ProjectId my-ai-companion-hub -Region europe-west1
```

`SERVICE_NAME` / `-ServiceName` default to `ai-companion-proxy`, which is the name Terraform uses.

## 3. Custom domain

The proxy is published on `custom_domain` through a [Cloud Run domain mapping](https://docs.cloud.google.com/run/docs/mapping-custom-domains#run).

> **Preview feature.** Cloud Run domain mapping is in Preview (limited availability) and Google does not recommend it for production services because of latency issues. It is available only in a subset of regions (`asia-east1`, `asia-northeast1`, `asia-southeast1`, `europe-north1`, `europe-west1`, `europe-west4`, `us-central1`, `us-east1`, `us-east4`, `us-west1` at the time of writing), so `region` must be one of them. In any other region, or if you need a production-grade setup, front the service with a global external Application Load Balancer instead and do not use this mapping. Check the [current documentation](https://docs.cloud.google.com/run/docs/mapping-custom-domains#run) for the up-to-date list.

**Domain ownership is verified outside Terraform.** Terraform cannot prove that you own the domain. Before the first `terraform apply`:

1. Open [Search Console](https://search.google.com/search-console) with the Google account (or service account) that runs Terraform and add the base domain as a property, for example `example.com` for `proxy.example.com`.
2. Complete the verification it proposes (usually a DNS `TXT` record at your registrar).
3. To let other people or service accounts create mappings on that domain, add them under **Verified owners** of the property.

After the apply, `terraform output custom_domain_dns_records` lists the DNS records to create at your registrar. The managed TLS certificate is issued once they are in place; it usually takes about 15 minutes and can take up to 24 hours. If you use CAA records, authorize both `pki.goog` and `letsencrypt.org`.

Cloud Run IAM accepts ID tokens whose audience is the service URL; when clients call a custom domain, configure the matching custom audience on the service, or have clients request tokens for the service URL.

## 4. Verify

```bash
URL=$(terraform -chdir=terraform output -raw proxy_service_uri)
TOKEN=$(gcloud auth print-identity-token)
curl -H "Authorization: Bearer $TOKEN" "$URL/health"
curl -H "Authorization: Bearer $TOKEN" "$URL/discovery"
```

Then run the live integration tests ([Development and testing](09-development-and-testing.md#live-integration-tests)) and check that an `inference_completed` event reaches BigQuery ([Observability](08-observability.md#verification)).

## Running the container yourself

Any container platform works as long as the environment variables of [Configuration](06-configuration.md) are provided. The image serves the Flask app `main:app` with Gunicorn (one worker, eight threads, no request timeout so long streams are not cut).

```bash
docker build -t ai-companion-proxy ./api
docker run --rm -p 8080:8080 --env-file proxy.env ai-companion-proxy
```

On Google Cloud outside Cloud Run you must provide runtime credentials with Vertex AI access (Application Default Credentials).

## Rolling out to a team

1. **Pilot.** Deploy the proxy, give a small group `roles/run.invoker`, and have them configure their clients to use it. Review logs, BigQuery events and the cost labels on the bill for about a week.
2. **Transition.** Distribute the client configuration to everyone (for example shared workspace settings) and add everyone to `proxy_invoker_identities`.
3. **Cut-over and hardening.** Remove direct Vertex AI permissions from users in the governed projects ([Security and IAM](03-security-and-iam.md#making-the-proxy-the-only-entry-point)). Verify that a direct call with a user's credentials now returns `403 PERMISSION_DENIED` while calls through the proxy still work.

## Upgrading

- Application changes: run the deploy script again.
- Configuration or infrastructure changes: edit `terraform.tfvars` or the `.tf` files, review `terraform plan`, then `terraform apply`.
- Renaming the proxy component or labels changes the log filter used by the metrics and sink. Terraform updates both together; events written by older revisions will not match the new filter.

## Optional: publish the API specification

`api/upload_openapi.sh` and `api/upload_openapi.ps1` download `/openapi.json` from the deployed proxy (authenticating with your gcloud identity) and upload it to [OpenAPI Viewer](https://www.openapiviewer.com) for an interactive view. They need the proxy URL (`PROXY_URL` / `-ProxyUrl`). Note that this sends the specification to a third-party service.
