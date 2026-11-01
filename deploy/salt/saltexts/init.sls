{#- SKETCH, untested. Installs/updates the pinned saltext wheels from the master file server. Install only: the
    orchestration (orch/saltexts/rollout.sls) owns restarts. #}
{%- set cfg    = salt['pillar.get']('saltexts', {}) %}
{%- set pins   = cfg.get('packages', {}) %}
{%- set pyver  = cfg.get('python', '3.11') %}
{%- set cpu    = grains['cpuarch']|lower %}
{%- set arch   = 'arm64' if cpu in ('aarch64','arm64') else ('x86_64' if cpu in ('x86_64','amd64') else 'unknown') %}
{%- set os_    = grains['kernel']|lower %}
{%- set target = os_ ~ '-' ~ arch %}
{%- set win    = (os_ == 'windows') %}
{%- set cache  = salt['file.join'](opts['cachedir'], 'saltext-wheels') %}
{%- set py     = grains['pythonexecutable'] %}
{%- set pyhave = grains['pythonversion'][0] ~ '.' ~ grains['pythonversion'][1] %}

{%- if target not in ['linux-x86_64','linux-arm64','windows-x86_64','windows-arm64'] %}
saltexts_unsupported_platform:
  test.fail_without_changes:
    - name: "saltexts: no wheelhouse for {{ target }}"
{%- elif pyhave != pyver %}
saltexts_python_mismatch:
  test.fail_without_changes:
    - name: "saltexts: wheels are built for Python {{ pyver }}, this minion's Salt has {{ pyhave }}"
{%- else %}
saltexts_wheels:
  file.recurse:
    - name: {{ cache }}
    - source: salt://saltexts/wheels/{{ target }}
    - clean: True
    - makedirs: True

saltexts_pins:
  file.managed:
    - name: {{ salt['file.join'](cache, 'pins.json') }}
    - contents: {{ pins|json|yaml_encode }}
    - require: [{file: saltexts_wheels}]

saltexts_helper:
  file.managed:
    - name: {{ salt['file.join'](cache, 'saltexts_apply.py') }}
    - source: salt://saltexts/files/saltexts_apply.py
    - require: [{file: saltexts_wheels}]

saltexts_apply:
  cmd.run:
    - name: '{{ "& " if win else "" }}"{{ py }}" "{{ salt["file.join"](cache, "saltexts_apply.py") }}" "{{ salt["file.join"](cache, "pins.json") }}" "{{ cache }}"'
    {%- if win %}
    - shell: powershell
    {%- endif %}
    - stateful: True
    - require: [{file: saltexts_pins}, {file: saltexts_helper}]
{%- endif %}
