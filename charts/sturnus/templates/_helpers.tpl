{{/*
Expand the name of the chart.
*/}}
{{- define "sturnus.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "sturnus.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "sturnus.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "sturnus.labels" -}}
helm.sh/chart: {{ include "sturnus.chart" . }}
{{ include "sturnus.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "sturnus.selectorLabels" -}}
app.kubernetes.io/name: {{ include "sturnus.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Component selector labels. Call with a dict: (dict "root" $ "component" "bot")
*/}}
{{- define "sturnus.componentSelectorLabels" -}}
{{ include "sturnus.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Component labels. Call with a dict: (dict "root" $ "component" "bot")
*/}}
{{- define "sturnus.componentLabels" -}}
{{ include "sturnus.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "sturnus.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "sturnus.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
The image reference shared by all three components.
*/}}
{{- define "sturnus.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) }}
{{- end }}

{{/*
The secret-derived environment for one component, rendered as `env` entries
with a per-key `valueFrom.secretKeyRef`. Call with a dict:
(dict "root" $ "component" "bot").

The three key lists live here, in one map, rather than being spelled out in
the three deployment templates: which process may see which credential is a
single security decision, and a reader or reviewer has to be able to check it
in one place instead of diffing three files that each look locally reasonable.
They deliberately do NOT live in values.yaml either -- this is not an operator
knob. A cluster values file able to add STURNUS_MASTER_KEY back onto `link`
would hand back exactly the property this construct exists to remove, and it
would do so silently, because the pod would still start.

Each list mirrors the settings class the corresponding entrypoint actually
instantiates -- sturnus.config.Settings for bot,
sturnus.entrypoints.worker.WorkerSettings, sturnus.entrypoints.link
.LinkSettings -- restricted to the fields that are secret material: every
SecretStr field, plus STURNUS_DATABASE_URL, which is a plain str in the model
but embeds the database password and so is treated as a credential regardless.
Everything else those classes require is non-secret and arrives through
`commonEnv` or the component's own `env` map.

What the split buys, concretely: `link` is the only component reachable from
outside the cluster -- the OAuth callback is published through a Cloudflare
Tunnel -- and it now receives STURNUS_DATABASE_URL and
STURNUS_OUTLINE_CLIENT_SECRET, and nothing else. It no longer receives
STURNUS_MASTER_KEY, the key that wraps every recording's data key and the
actual asset to protect; nor STURNUS_DISCORD_TOKEN; nor the S3 credentials
that would grant direct access to the encrypted objects. It never read any of
them -- LinkSettings does not declare them -- so `envFrom` was handing the
internet-facing process the entire recording archive's keys for nothing.

No entry sets `optional: true`, deliberately. A key missing from the Secret
therefore stops the pod at CreateContainerConfigError, before the container
runs at all, instead of injecting an empty string. That is the safer failure:
pydantic accepts "" for a required SecretStr, so an absent STURNUS_MASTER_KEY
under `optional: true` would produce a bot that starts cleanly and wraps every
session's data key under an empty key -- unnoticed until the recordings need
to be read back. Refusing to start is the loud version of the same event, and
`kubectl describe pod` names the missing key.
*/}}
{{- define "sturnus.secretEnv" -}}
{{- /*
  STURNUS_SENTRY_DSN is on all three lists, and it is the one key here that
  is not a secret in the usual sense: it carries Sentry's *public* key,
  write-only to one project and granting no read access. It is routed
  through the Secret anyway, because the GitOps repository that supplies
  these values is public, and a DSN published there can be used by anyone
  who finds it to fill the project with events. Being unable to read
  anything is not the same as being harmless to publish.

  It is also the one key that may legitimately be empty: blank is the off
  switch (`SentrySettings._blank_dsn_is_absent`). It is still required to be
  *present*, like every other key here -- see the `optional` note below --
  so a cluster that does not report errors sets it to the empty string
  rather than omitting it.
*/ -}}
{{- $lists := dict
      "bot" (list "STURNUS_DISCORD_TOKEN" "STURNUS_DATABASE_URL" "STURNUS_S3_ACCESS_KEY" "STURNUS_S3_SECRET_KEY" "STURNUS_MASTER_KEY" "STURNUS_SENTRY_DSN")
      "worker" (list "STURNUS_DATABASE_URL" "STURNUS_S3_ACCESS_KEY" "STURNUS_S3_SECRET_KEY" "STURNUS_MASTER_KEY" "STURNUS_OUTLINE_SERVICE_KEY" "STURNUS_SENTRY_DSN")
      "link" (list "STURNUS_DATABASE_URL" "STURNUS_OUTLINE_CLIENT_SECRET" "STURNUS_SENTRY_DSN")
-}}
{{- if not (hasKey $lists .component) -}}
{{- fail (printf "sturnus.secretEnv: no secret key list defined for component %q" .component) -}}
{{- end -}}
{{- /*
  Scoping the secretKeyRefs is only half the property. A values file can
  still put a credential on a component as a literal, through either that
  component's own `env` map or the shared `commonEnv` --
  `--set link.env.STURNUS_MASTER_KEY=...` renders the master key in plain
  text on the internet-facing container, and via `commonEnv` on all three
  at once. The pod starts either way, so nothing signals it. Refuse to
  render instead: an operator who needs a credential somewhere new adds it
  to the list above, where the decision is reviewed, rather than routing
  around it from a cluster values file.

  Both maps are checked against the union of all three components' lists,
  not just this component's, so a credential cannot be smuggled onto one
  component by naming a key another component legitimately reads.
*/ -}}
{{- $componentEnv := (index $.root.Values .component | default dict).env | default dict -}}
{{- $shared := $.root.Values.commonEnv | default dict -}}
{{- range $all := (concat (index $lists "bot") (index $lists "worker") (index $lists "link")) -}}
{{- if hasKey $componentEnv $all -}}
{{- fail (printf "sturnus.secretEnv: %s.env sets the credential %s as a plain env value. Credentials reach a component only through the Secret named by existingSecret; add it to the key list in _helpers.tpl if this component genuinely needs it." $.component $all) -}}
{{- end -}}
{{- if hasKey $shared $all -}}
{{- fail (printf "sturnus.secretEnv: commonEnv sets the credential %s as a plain env value, which would place it on all three components at once. Credentials reach a component only through the Secret named by existingSecret." $all) -}}
{{- end -}}
{{- end -}}
{{- range (index $lists .component) }}
- name: {{ . }}
  valueFrom:
    secretKeyRef:
      name: {{ $.root.Values.existingSecret }}
      key: {{ . }}
{{- end }}
{{- end }}
