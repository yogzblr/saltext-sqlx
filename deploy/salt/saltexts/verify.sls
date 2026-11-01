{#- Proves the expected extension modules are loaded in the RUNNING minion (a module whose extension or driver is missing is not).
    Pillar saltexts:modules lists virtual module names, e.g. [sqlx, kafka]. #}
{%- set want = salt['pillar.get']('saltexts:modules', []) %}
{%- set have = salt['sys.list_modules']() %}
{%- for m in want if m not in have %}
saltexts_missing_{{ m }}:
  test.configurable_test_state:
    - result: False
    - changes: False
    - comment: {{ ("module '" ~ m ~ "' is not loaded (extension or driver missing, or restart pending)")|tojson }}
{%- endfor %}
saltexts_verified:
  test.succeed_without_changes:
    - name: all expected saltext modules are loaded
