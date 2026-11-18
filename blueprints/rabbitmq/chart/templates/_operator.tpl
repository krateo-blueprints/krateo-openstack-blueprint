{{/*
Krateo (#309 H2b): the broker as a RabbitmqCluster (operator.enabled), instead of this chart's StatefulSet.

rabbitmq.chart_mode is "true" unless operator.enabled. Every chart-mode template guards on it, so in operator mode the
chart renders only templates/rabbitmqcluster.yaml (and the generic registry objects). hack/validate-messaging-operator.py
renders both modes.
*/}}

{{- define "rabbitmq.chart_mode" -}}
{{- if not (.Values.operator | default dict).enabled }}true{{ end -}}
{{- end -}}

{{/* "true" when this render runs against a live cluster: core-provider's own CRD is the witness, the same way the
     umbrella decides whether its prerequisite checks can be measured. Offline (helm template, the gates) it is "". */}}
{{- define "rabbitmq.operator_live" -}}
{{- if lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "compositiondefinitions.core.krateo.io" }}true{{ end -}}
{{- end -}}

{{/* The RabbitmqCluster CRD, as JSON ({} when absent). The only lookup of it: the gate replaces this expression. */}}
{{- define "rabbitmq.operator_crd" -}}
{{- lookup "apiextensions.k8s.io/v1" "CustomResourceDefinition" "" "rabbitmqclusters.rabbitmq.com" | toJson -}}
{{- end -}}

{{/* This chart's own chart-mode StatefulSet, as JSON ({} when absent). The same lookup _erlang_cookie.tpl makes, so
     the render identity already may read it. The gate replaces this expression. */}}
{{- define "rabbitmq.operator_chart_sts" -}}
{{- lookup "apps/v1" "StatefulSet" .Release.Namespace (printf "%s-rabbitmq" .deployment_name) | toJson -}}
{{- end -}}
