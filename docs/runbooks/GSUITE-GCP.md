# Running the scope model on Google Workspace + Google Cloud

How the configurable scope hierarchy (`context/scopes.json`, see
`CONTEXT-ARCHITECTURE.md`) maps onto a company that lives in GSuite and GCP.
The principle stays the same — **sharing is structural, siloing is structural,
PII is substituted not trusted** — but every control gets a managed Google
implementation.

## Scope ↔ Workspace mapping

| Scope level | Content source of truth | Identity (who may invoke) |
|---|---|---|
| Enterprise | Org-wide **Shared Drive** ("Company Handbook") | Domain / `agents-all@…` Group |
| Department | Department **Shared Drive** | Department **Google Group** |
| Team | Team **Shared Drive** or folder | Team Google Group |
| Personal | User's **My Drive** folder | The user |
| Client (consulting) | Client Shared Drive, membership = engagement staff | Engagement Google Group |

- **Google Groups are the scope registry's identity layer**: `identityGroup` in
  `scopes.json` names a Group; n8n (or IAP, below) checks the caller's
  membership via the Admin SDK Directory API before a `/route` call carries
  that scope. Group nesting mirrors scope nesting (team group is a member of
  department group), so access rolls up the same way context does.
- **Sync, don't mount, Drive**: an n8n scheduled workflow (native Google Drive
  node) pulls each scope's Drive content into `workspace/scopes/<path>/` —
  **through the ingest scrubber first** (below). Drive stays the human-facing
  editing surface; the workspace tree stays the agent-facing one. Deliverables
  flow back the other way (agent writes to `deliverables/`, n8n uploads to the
  scope's Drive).
- **Drive labels** (Workspace data classification) mark documents `pii`,
  `client-confidential`, etc.; the sync workflow reads labels and routes
  labeled docs to the stricter pipeline (or refuses to sync them at all).

## PII pipeline → Cloud DLP (Sensitive Data Protection)

Replace/augment the Presidio option with Google's managed service:

1. **Ingest scrub**: the Drive→workspace sync calls
   `projects.content.deidentify` with a **de-identify template** using
   `CryptoReplaceFfxFpeConfig` (format-preserving, deterministic) or
   `CryptoDeterministicConfig` — token keys wrapped by **Cloud KMS**. Output is
   coherent text with reversible surrogates (`EMAIL(52):AbCd…`), same shape as
   the Presidio `«EMAIL_001»` pattern.
2. **Rehydration**: a post-model n8n step calls `content.reidentify` with the
   same template/key. The KMS key's IAM policy is the vault: **runner service
   accounts get no `dlp.kms` access** — only the rehydration step's service
   account does. The model physically cannot un-tokenize.
3. **Prompt-time guard**: the router's regex scan stays (free, local); for
   NER-grade coverage, point the guard at `content.inspect` with an inspect
   template per scope (`infoTypes` tuned to the scope's `pii` control).
4. **Storage-wide sweeps**: DLP **inspection jobs** over the GCS buckets (below)
   catch anything that slipped in — scheduled, with findings to Security
   Command Center.

## Infrastructure mapping (GCP)

| Framework piece | GCP implementation |
|---|---|
| Runner containers | **Cloud Run** services (pool) / one service per silo scope; or GKE Autopilot with a namespace per silo. Images in **Artifact Registry**. |
| Runner identity | One **service account per scope tier** via Workload Identity — least-privilege IAM is the mount table's cloud twin. |
| iptables egress firewall | **VPC firewall egress rules + Cloud NAT** (GKE) or serverless VPC connector with restricted egress (Cloud Run). Same default-deny, allowlist per runner. |
| Data perimeter | **VPC Service Controls** perimeter around DLP, GCS, Vertex AI, Secret Manager — API-level exfiltration guard even if credentials leak. |
| Workspace tree storage | **GCS bucket per scope** (uniform bucket-level access, **CMEK**) mirrored to the pod/container; offboarding = delete bucket + KMS key disable. |
| Token vault / secrets | **Secret Manager** (+ KMS). `RUNNER_TOKEN`s, API keys, DLP key wrappers. Never in env files. |
| Audit trail | Router `decision` blocks + **Cloud Audit Logs** shipped to **BigQuery**; dashboard in Looker Studio. agentsview stays as the local/dev lens. |
| Event triggers (AG-3) | Workspace events → **Pub/Sub** → n8n webhook: Drive changes, Gmail (via watch), Chat messages, Cloud Monitoring alerts. |
| Human gates | n8n approval steps via **Google Chat** interactive cards or Gmail. |
| Caller auth to n8n | **Identity-Aware Proxy** in front of the n8n UI/webhooks — Google SSO + Group checks before anything reaches the router. |

## Notifications

The notify seam already fits: **Google Chat incoming webhooks accept `{"text"}`**,
so `NOTIFY_CHANNEL=webhook` + a Chat space webhook URL works with zero changes.
(Add a dedicated `chat` branch to the Notify sub-workflow later if you want
Chat-card formatting.)

## Model lanes

- **gemini-runner** gets a first-class home: authenticate with a **Vertex AI
  service account** instead of the consumer free tier — Vertex keeps prompts and
  data inside your GCP project and VPC-SC perimeter, which is what client
  confidentiality actually requires. (The consumer free tier is fine for the
  personal/dev scope lane; use Vertex for client/department lanes.)
- **claude-runner** unchanged (subscription CLI) — or Claude on **Vertex AI
  Model Garden** if the org wants a single GCP billing/perimeter story; note
  that path is metered, so it trades the flat-rate advantage for governance.

## Rollout order (pragmatic)

1. Groups per scope + `identityGroup` filled in `scopes.json`; IAP in front of n8n.
2. Drive→workspace sync workflows with DLP de-identify on ingest (start with
   one department).
3. Router PII guard pointed at DLP inspect for `pii: "block"` scopes.
4. Silo runners for walled clients as separate Cloud Run services w/ per-service
   egress + service accounts.
5. VPC-SC perimeter + audit-to-BigQuery once the shape settles.
