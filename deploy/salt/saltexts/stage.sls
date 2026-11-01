{#- Stage: copy the wheelhouse folder for this platform from the master, and the helper + pins. Installs nothing. #}
{%- from "saltexts/map.jinja" import m with context %}
{%- if m.guard %}
{{ m.guard[0] }}:
  test.configurable_test_state:
    - result: False
    - changes: False
    - comment: {{ m.guard[1]|tojson }}
{%- else %}
saltexts_wheels:
  file.recurse:
    - name: {{ m.cache|tojson }}
    - source: salt://{{ m.root }}/{{ m.target }}
    - clean: True
    - makedirs: True
    {%- if not m.win %}
    - dir_mode: '0755'
    - file_mode: '0644'
    {%- endif %}

saltexts_state_dir:
  file.directory:
    - name: {{ m.sdir|tojson }}
    - makedirs: True
    {%- if not m.win %}
    - mode: '0750'
    {%- endif %}

saltexts_pins:
  file.managed:
    - name: {{ m.pinsf|tojson }}
    - contents: {{ (m.pins|tojson(sort_keys=True))|tojson }}
    - require: [{file: saltexts_state_dir}]

saltexts_helper:
  file.managed:
    - name: {{ m.helper|tojson }}
    - source: salt://saltexts/files/saltexts_apply.py
    - require: [{file: saltexts_state_dir}]
{%- endif %}
