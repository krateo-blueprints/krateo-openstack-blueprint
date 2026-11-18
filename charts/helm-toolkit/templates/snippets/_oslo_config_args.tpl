{{/*
Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/}}

{{/*
abstract: |
  A program's oslo.config arguments, rendered from ONE ordered list for both of their consumers: the
  start script's exec line ("format" "shell") and the worker readiness probe's exec command ("format"
  "probe", a JSON array). One source by construction (#231): the probe reads exactly the configuration
  the program was started with, and nothing reads it back from the program's /proc cmdline, which
  oslo.service's threading backend and cotyledon rewrite into a title.

  The list is the chart's own definition, `<chart>.config_args.<program>`, a YAML list in the order
  oslo.config must read it (#228's drop-ins are last). Each entry is one of
    file: PATH            --config-file PATH
    dir: PATH             --config-dir PATH
    dirIfPresent: PATH    --config-dir PATH only if the directory exists when the program starts
                          (shell: `$([ -d PATH ] && echo --config-dir PATH)`; the probe checks the same
                          directory when it runs, --config-dir-if-present)
  Template conditionals live in that definition, so both consumers see the same entries.

  "shell": one argument per line, each `<indent>ARG \`; the last line has no backslash unless
  "continued" is true (more arguments follow on the exec line). No leading or trailing newline.
values: |
  (none)
usage: |
  exec designate-central \
  {{ include "helm-toolkit.snippets.oslo_config_args" (dict "args" (include "designate.config_args.central" . | fromYamlArray) "format" "shell" "indent" 6) }}
return: |
        --config-file /etc/designate/designate.conf \
        --config-dir /etc/designate/designate.conf.d
*/}}

{{- define "helm-toolkit.snippets.oslo_config_args" -}}
{{- $args := index . "args" | default list -}}
{{- $format := index . "format" -}}
{{- if not $args -}}
{{- fail "helm-toolkit.snippets.oslo_config_args: the configuration list is empty" -}}
{{- end -}}
{{- $words := list -}}
{{- $probe := list -}}
{{- range $e := $args -}}
{{- if not (kindIs "map" $e) -}}
{{- fail (printf "helm-toolkit.snippets.oslo_config_args: entry %v is not a map with file, dir or dirIfPresent" $e) -}}
{{- end -}}
{{- if ne (len $e) 1 -}}
{{- fail (printf "helm-toolkit.snippets.oslo_config_args: entry %v must have exactly one of file, dir or dirIfPresent" $e) -}}
{{- end -}}
{{- if hasKey $e "file" -}}
{{- $words = append $words (printf "--config-file %s" $e.file) -}}
{{- $probe = concat $probe (list "--config-file" $e.file) -}}
{{- else if hasKey $e "dir" -}}
{{- $words = append $words (printf "--config-dir %s" $e.dir) -}}
{{- $probe = concat $probe (list "--config-dir" $e.dir) -}}
{{- else if hasKey $e "dirIfPresent" -}}
{{- $words = append $words (printf "$([ -d %s ] && echo --config-dir %s)" $e.dirIfPresent $e.dirIfPresent) -}}
{{- $probe = concat $probe (list "--config-dir-if-present" $e.dirIfPresent) -}}
{{- else -}}
{{- fail (printf "helm-toolkit.snippets.oslo_config_args: entry %v has none of file, dir or dirIfPresent" $e) -}}
{{- end -}}
{{- end -}}
{{- if eq $format "shell" -}}
{{- $pad := repeat (int (index . "indent" | default 6)) " " -}}
{{- $last := sub (len $words) 1 -}}
{{- range $i, $w := $words -}}
{{- if $i }}{{ "\n" }}{{ end -}}
{{ $pad }}{{ $w }}{{ if or (lt $i $last) (index $ "continued") }} \{{ end }}
{{- end -}}
{{- else if eq $format "probe" -}}
{{- $probe | toJson -}}
{{- else -}}
{{- fail (printf "helm-toolkit.snippets.oslo_config_args: format %q is neither shell nor probe" $format) -}}
{{- end -}}
{{- end -}}
