#!/usr/bin/env bash
# Arrange manually unpacked data, then invoke the existing experiment code.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
PYTHON=${PYTHON:-python}
WORK=${PIIBENCH_WORK_DIR:-"$ROOT/outputs/reproduction_inputs"}
BASE_URL=${PIIBENCH_BASE_URL:-http://127.0.0.1:8000}
case "$WORK" in /*) ;; *) WORK="$ROOT/$WORK" ;; esac

die() { printf '%s\n' "$*" >&2; exit 1; }
need_file() { [ -f "$1" ] || die "Missing file: $1"; }
config_json() {
    "$PYTHON" -c 'import json,sys; from defipi.config import load_config_file; print(json.dumps(load_config_file(sys.argv[1])))' "$1"
}
help() {
    cat <<'HELP'
Usage:
  bash scripts/experiments.sh prepare /path/to/unpacked
  bash scripts/experiments.sh rq1 prepare --output outputs/rq1 [existing options]
  bash scripts/experiments.sh rq1 plan|run|score-prepare|judge|report [existing options]
  bash scripts/experiments.sh rq2|rq3|rq4 [existing options]

prepare needs only evaluation_data/ and optimization_results/.
Unpack the ZIPs manually first.
PYTHON selects the installed Python environment; PIIBENCH_WORK_DIR selects a
new working directory inside this repository. PIIBENCH_BASE_URL sets the public
origin at which the generated evaluation/site/ will be hosted for URL runs.
prepare is offline. RQ run and judge commands can make model requests.
Use scripts/train.sh for training and optimization.
HELP
}

prepare_evaluation() {
    [ "$#" -eq 1 ] || die "prepare requires the directory containing evaluation_data/ and optimization_results/"
    command -v jq >/dev/null || die "Install jq before running this script"
    local input scenario method pages source out neutral
    input=$(CDPATH= cd -- "$1" && pwd)
    need_file "$input/evaluation_data/gold.json"
    need_file "$input/optimization_results/index.json"
    [ ! -e "$WORK/evaluation" ] || die "Evaluation output already exists; choose a new PIIBENCH_WORK_DIR: $WORK"
    case "$WORK/" in "$ROOT/"*) ;; *) die "PIIBENCH_WORK_DIR must be inside this repository" ;; esac
    mkdir -p "$WORK/evaluation/site"
    for scenario in academic news; do
        pages="$input/evaluation_data/$scenario/pages.json"
        need_file "$pages"
        need_file "$input/evaluation_data/$scenario/tasks.json"
        for method in noguard gepa piiguard autoguard webcloak muzzle; do
            printf 'Applying %s / %s directly to NoGuard HTML\n' "$method" "$scenario"
            source="$input/optimization_results/$method/$scenario"
            out="$WORK/export/$method/$scenario"
            neutral="$WORK/neutral/$method/$scenario"
            # The codebase's HTML operators are Python functions. This invocation
            # applies them to the supplied HTML; it loads no source records or IDs.
            "$PYTHON" - "$method" "$pages" "$input/evaluation_data/$scenario/tasks.json" "$source" "$out" <<'APPLY'
import json, sys
from pathlib import Path
from bs4 import BeautifulSoup
from defipi.benchmark.files import load_pages, load_tasks
from defipi.artifacts import write_artifact
from defipi.methods.common.academic_inject import inject_academic_defense
from defipi.methods.common.defense_fragments import normalize_fragment_spec, render_defense_fragment, build_fragment_table_rows
from defipi.methods.common.ax_edits import compile_news_ax_edits
from defipi.methods.autoguard.injection import inject_static
from defipi.methods.muzzle.injection import inject_notice
from defipi.methods.webcloak.core import prepare_groups

method, page_path, task_path, source, output = sys.argv[1:]
pages, tasks, source = load_pages(page_path), load_tasks(task_path), Path(source)
truth = {task['page_id']: task['reporter_info'] for task in tasks['tasks']}
assert set(truth) == {p['page_id'] for p in pages['pages']}
def read(name): return json.loads((source / name).read_text())
candidate = read('selected_candidate.json') if method in ('gepa', 'piiguard', 'autoguard', 'muzzle') else None
rows = read('per_page_results.json') if method in ('webcloak', 'muzzle') else []
results = {row['page_id']: row for row in rows}
if rows:
    assert len(results) == len(rows) and set(results) == set(truth)

def news_fragment(html, candidate):
    fragment = normalize_fragment_spec(candidate['defense_fragment'])
    if fragment.get('format') == 'ax_edit_v1':
        html, _ = compile_news_ax_edits(html, fragment)
    soup = BeautifulSoup(html, 'html.parser')
    slot = fragment['slot']
    extra_rows = build_fragment_table_rows(fragment)
    if extra_rows:
        table = soup.select_one('section.contact-block table.meta-table')
        if table is None: raise ValueError('Missing News contact table')
        for cells in extra_rows:
            row = soup.new_tag('tr')
            for text in cells:
                cell = soup.new_tag('td'); cell.string = text; row.append(cell)
            table.append(row)
    else:
        node = BeautifulSoup(render_defense_fragment(fragment), 'html.parser').find()
        if slot in ('bio_tail', 'contact_block'):
            anchor = soup.select_one('section.contact-block')
            if anchor is None: raise ValueError('Missing News contact block')
            anchor.insert_before(node) if slot == 'bio_tail' else anchor.append(node)
        else:
            name = 'header' if slot == 'header_notice' else 'footer'
            anchor = soup.find(name)
            if anchor is None:
                anchor = soup.new_tag(name); soup.body.append(anchor)
            anchor.append(node)
    return str(soup)

rendered, files = [], {}
for page in pages['pages']:
    page_id, html = page['page_id'], page['clean_html']
    if pages['scenario'] == 'academic' and method in ('gepa', 'webcloak'):
        # Existing source-layout validation expects this marker spelling.
        # It does not change the supplied document body or contact values.
        html = html.replace('data-sidebar-layout="single-sidebar-v1"',
            'data-ax-edit-single-sidebar-layout="academic-contact-single-sidebar-layout-v1"')
    if method in ('gepa', 'piiguard'):
        chosen = candidate if method == 'gepa' else candidate['candidate']
        if pages['scenario'] == 'academic':
            html = inject_academic_defense(html, position=chosen['position'],
                attack_instruction=chosen.get('attack_instruction', ''), defense_fragment=chosen['defense_fragment'])
        else:
            html = news_fragment(html, chosen)
    elif method == 'autoguard':
        html = inject_static(html, candidate['text'])
    elif method == 'muzzle':
        html = inject_notice(html, candidate['best']['surface'], results[page_id]['notice'], page_profile=pages['scenario'])
    elif method == 'webcloak':
        assets = {}
        html = prepare_groups(html, results[page_id]['semantics'], {'reporter_info': truth[page_id]},
            rng_seed=42, semantic_rng_seed=42, assets=assets, page_profile=pages['scenario'])['Full WebCloak-PII']
        for name, content in assets.items():
            files[(Path(page['document']).parent / name).as_posix()] = content
    rendered.append({'page_id': page_id, 'html': html})
write_artifact(output, pages, {'pages': rendered, 'files': files, 'defense_variant': method},
    method_id=method, artifact_id=f"{method}-{pages['scenario']}")
APPLY
            "$PYTHON" scripts/neutralize_pages.py --artifact "$out/artifact.json" --output "$neutral" --base-url "$BASE_URL"
            mkdir -p "$WORK/evaluation/$method/$scenario"
            cp -R "$neutral/site/." "$WORK/evaluation/site/"
            jq '.site_root="../../site"' "$neutral/artifact.json" > "$WORK/evaluation/$method/$scenario/artifact.json"
            cp "$input/evaluation_data/$scenario/tasks.json" "$WORK/evaluation/$method/$scenario/tasks.json"
        done
    done
    cp "$input/evaluation_data/gold.json" "$WORK/evaluation/gold.json"
    printf '%s\n' "$input" > "$WORK/.evaluation-ready"
    printf 'Prepared evaluation using only NoGuard pages and optimization results: %s/evaluation\n' "$WORK"
}


action=${1:---help}
[ "$#" -eq 0 ] || shift
case "$action" in
  --help|-h|help) help ;;
  prepare) prepare_evaluation "$@" ;;
  rq1|rq2|rq4)
    need_file "$WORK/.evaluation-ready"
    if [ "${1:-}" = prepare ]; then
        shift
        exec "$PYTHON" -m "scripts.$action" prepare --dataset "$WORK/evaluation" "$@"
    fi
    exec "$PYTHON" -m "scripts.$action" "$@"
    ;;
  rq3) exec "$PYTHON" -m scripts.rq3 "$@" ;;
  *) help >&2; die "Unknown command: $action" ;;
esac
