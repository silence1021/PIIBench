#!/usr/bin/env bash
# Arrange released training inputs and invoke the existing native optimizers.
set -euo pipefail
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
PYTHON=${PYTHON:-python}
WORK=${PIIBENCH_WORK_DIR:-"$ROOT/outputs/reproduction_inputs"}
case "$WORK" in /*) ;; *) WORK="$ROOT/$WORK" ;; esac
die() { printf '%s\n' "$*" >&2; exit 1; }
need_file() { [ -f "$1" ] || die "Missing file: $1"; }
config_json() {
    "$PYTHON" -c 'import json,sys; from defipi.config import load_config_file; print(json.dumps(load_config_file(sys.argv[1])))' "$1"
}
help() {
    cat <<'HELP'
Usage:
  bash scripts/train.sh --help|-h|help
  bash scripts/train.sh prepare /path/to/manually-unpacked-data
  bash scripts/train.sh run gepa|piiguard|webcloak SCENARIO
  bash scripts/train.sh run muzzle SCENARIO
  bash scripts/train.sh run autoguard SCENARIO --authorize-paid-calls
  bash scripts/train.sh select-gepa

SCENARIO: academic or news.
MUZZLE reads independent_runs, search.iterations and search budgets from
configs/muzzle-SCENARIO.json under the working directory. Confirmation reads
repeats and budgets from configs/muzzle-SCENARIO-confirmation.json there.
The Shell executes all configured trials; each keeps its independent budgets.

Manually extract training_data.zip and evaluation_data.zip as described in
README before using this script. The script does not extract ZIPs.
prepare arranges extracted files, writes training configurations and binds the
saved AutoGuard questions to pages. It makes no model requests and runs once
per working directory. No full_scope is read.
run starts only the selected method/scenario. Required validation runs inside
the native training entry; MUZZLE also checks its Pi/container runtime.
select-gepa performs GEPA News candidate selection after its search, including
its internal preparation and report. MUZZLE run includes search followed by
same-page confirmation, with separate stage budgets and output directories.
run and select-gepa can make model requests.

PYTHON selects the interpreter (default python).
PIIBENCH_WORK_DIR selects a new working directory inside this repository
(default outputs/reproduction_inputs). Models, provider routes, budgets and
concurrency are set in the generated JSON files under that directory's configs/.
Provider API keys come from the environment or .env.

HELP
}
scenario_arg() {
    [ "$#" -eq 1 ] || die "Expected one scenario: academic or news"
    case "$1" in academic|news) ;; *) die "Expected academic or news" ;; esac
}

check_muzzle_runtime() {
    local image=${1:-piiguard-agents:chromium156} resolved
    "$PYTHON" - <<'HOST'
import json
from pathlib import Path
from importlib.metadata import version
from defipi.paths import PROJECT_ROOT
from defipi.methods.muzzle.author import AuthorMuzzlePII
from muzzle.prototype.agents import pair
from PAIR import conversers
vendor = PROJECT_ROOT / "third_party/muzzle"
for module in (pair, conversers):
    if not Path(module.__file__).resolve().is_relative_to(vendor.resolve()):
        raise RuntimeError("MUZZLE training must load the bundled MUZZLE/PAIR sources")
print(json.dumps({"muzzle_training_controller": "ready", "author_sources": "bundled MUZZLE and PAIR",
                  "autogen_core": version("autogen-core")}, indent=2))
HOST
    command -v docker >/dev/null || die "Install Docker to run the MUZZLE Pi training trajectories"
    resolved=$(docker image inspect --format '{{.Id}}' "$image")
    # This starts no provider client and needs no API key. The same built image
    # used by PiBrowserRunner must contain Pi, MCP and a working Chromium.
    docker run --rm -i --network none --entrypoint python "$resolved" - <<'CONTAINER'
import json, subprocess
from pathlib import Path
from defipi.execution.runtime_paths import executable
root = Path("/opt/runtime/node_modules")
pi = subprocess.run([executable("pi"), "--version"], check=True, capture_output=True, text=True, timeout=30)
mcp = json.loads((root / "@playwright/mcp/package.json").read_text())["version"]
version = subprocess.run(["/usr/bin/chromium", "--version"], check=True, capture_output=True, text=True, timeout=15)
page = subprocess.run(["/usr/bin/chromium", "--headless", "--no-sandbox", "--disable-gpu",
                       "--disable-dev-shm-usage", "--dump-dom",
                       "data:text/html,<h1>MUZZLE_PI_RUNTIME_OK</h1>"],
                      check=True, capture_output=True, text=True, timeout=30)
if "MUZZLE_PI_RUNTIME_OK" not in page.stdout:
    raise RuntimeError("Chromium did not render the local probe page")
print(json.dumps({"muzzle_pi_container": "ready", "pi": pi.stdout.strip(), "playwright_mcp": mcp,
                  "browser": version.stdout.strip(), "browser_render": "passed", "model_requests": 0}, indent=2))
CONTAINER
}

run_muzzle_trial() {
    local config=$1 scenario=$2 run_index=$3 image_id=$4 confirmation_template=$5 batch_output=$6
    local run_name search_output search_exit
    run_name=$(printf 'run-%02d' "$run_index")
    search_output="$batch_output/$run_name/search"
    jq --arg image "$image_id" --arg out "$search_output" \
      '.independent_runs=1 | .pi_agent.container_image=$image | .output=$out' "$config" > "$WORK/configs/muzzle-$scenario-$run_name.json" || return $?
    search_exit=0
    "$PYTHON" -m cli.muzzle_pii run --config "$WORK/configs/muzzle-$scenario-$run_name.json" || search_exit=$?
    # A bounded search can exhaust its budget after finding a validated
    # candidate. The native confirmation accepts that recorded outcome.
    if [ ! -f "$search_output/result.json" ]; then
        [ "$search_exit" -eq 0 ] || return "$search_exit"
        printf '%s\n' "MUZZLE search did not write a result; confirmation was not started" >&2
        return 1
    fi
    if [ "$search_exit" -ne 0 ] && ! jq -e '.status=="budget_exhausted"' "$search_output/result.json" >/dev/null; then
        return "$search_exit"
    fi
    if ! jq -e '(.status=="completed" or .status=="budget_exhausted") and
                (.pages|length>0) and all(.pages[]; .best != null)' \
         "$search_output/result.json" >/dev/null; then
        printf '%s\n' "MUZZLE search did not export a validated candidate for confirmation; see $search_output/result.json" >&2
        [ "$search_exit" -eq 0 ] || return "$search_exit"
        return 1
    fi
    if [ "$search_exit" -ne 0 ]; then
        printf '%s\n' "MUZZLE search reached its budget; continuing with its validated candidate"
    fi
    jq --arg src "$search_output" --arg out "$batch_output/$run_name/confirmation" \
      '.search_output=$src | .output=$out' "$confirmation_template" > "$WORK/configs/muzzle-$scenario-$run_name-confirmation.json" || return $?
    printf 'Confirming MUZZLE %s / %s with the separate confirmation budget\n' "$scenario" "$run_name"
    "$PYTHON" -m cli.muzzle_pii confirm --config "$WORK/configs/muzzle-$scenario-$run_name-confirmation.json"

}

prepare_training() {
    [ "$#" -eq 1 ] || die "prepare requires training_data/ and evaluation_data/"
    command -v jq >/dev/null || die "Install jq before running this script"
    local input scenario name source configs pages base
    input=$(CDPATH= cd -- "$1" && pwd)
    for name in training_data evaluation_data; do
        [ -d "$input/$name" ] || die "Missing unpacked directory: $input/$name"
    done
    need_file "$input/training_data/index.json"
    need_file "$input/evaluation_data/gold.json"
    [ ! -e "$WORK/data" ] || die "Training data already exists; choose a new PIIBENCH_WORK_DIR: $WORK"
    case "$WORK/" in "$ROOT/"*) ;; *) die "PIIBENCH_WORK_DIR must be inside this repository" ;; esac
    "$PYTHON" -c 'import bs4,yaml; import defipi.methods.webcloak.transfer'
    mkdir -p "$WORK/data/benchmark" "$WORK/configs"
    configs="$WORK/configs"
    cp "$input/training_data/training_prompts.json" "$WORK/data/training_prompts.json"

    for scenario in academic news; do
        printf 'Preparing %s training pages and NoGuard split checks\n' "$scenario"
        mkdir -p "$WORK/data/$scenario/html" "$WORK/data/benchmark/$scenario/site"
        cp -R "$input/training_data/$scenario/train25/site/." "$WORK/data/$scenario/html/"
        cp -R "$input/evaluation_data/$scenario/site/." "$WORK/data/benchmark/$scenario/site/"
        # Existing split checks expect held-out records alongside development
        # records. Build those check-only records from the supplied NoGuard HTML,
        # using page_id itself. There is no original-corpus ID lookup.
        "$PYTHON" - "$input" "$WORK" "$scenario" <<'BIND'
import json, sys
from pathlib import Path
from bs4 import BeautifulSoup
from defipi.benchmark.files import load_pages, load_tasks
from defipi.benchmark.splits import load_seed_records
from defipi.config import load_config_file
from defipi.methods.common.render import render_seed_html
source, work, scenario = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
train = source / "training_data" / scenario / "train25"
evaluation = source / "evaluation_data" / scenario
pages = load_pages(evaluation / "pages.json")
tasks = load_tasks(evaluation / "tasks.json")
by_page = {task["page_id"]: task for task in tasks["tasks"]}
records = load_seed_records(train / "seeds.json", start_index=0, limit=None, table_seed=42)
split = json.loads((train / "split.json").read_text())
if len(records) != 25 or {r["id"] for r in records} != set(split["training_record_ids"]):
    raise ValueError("Expected the saved 25 training pages")
# Reuse the current native News renderer, as PIIGuard's own prepare-data does.
# The extracted HTML and ZIP remain untouched.
if scenario == "news":
    for record in records:
        target = work / "data/news/html" / record["html_file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_seed_html(record))
heldout = []
for page in pages["pages"]:
    task = by_page[page["page_id"]]
    html = (evaluation / "site" / page["document"]).read_text()
    if scenario == "news":
        article = BeautifulSoup(html, "html.parser").select_one("article.context")
        if article is None:
            raise ValueError("NoGuard News page has no article.context")
        context = "\n\n".join(p.get_text() for p in article.find_all("p", recursive=False))
        if not context.strip():
            raise ValueError("NoGuard News article is empty")
    else:
        context = html
    truth = task["reporter_info"]
    heldout.append({"id": page["page_id"], "context": context,
                    "question": task["question"], "reporter_info": truth,
                    "rendered_reporter_info": truth,
                    "html_file": f"../../benchmark/{scenario}/site/{page['document']}"})
    # Legacy native checks name this field source_record_id. Its value is the
    # supplied page_id, not an ID recovered from full_scope or another corpus.
    task["metadata"]["source_record_id"] = page["page_id"]
ids = split["training_record_ids"]
webcloak_ids = load_config_file(f"configs/methods/webcloak-{scenario}.yaml")["data"]["developer_ids"]
muzzle_ids = load_config_file("configs/methods/muzzle-news.yaml" if scenario == "news" else "configs/methods/muzzle.yaml")["data"]["developer_ids"]
if not set(webcloak_ids + muzzle_ids) <= set(ids):
    raise ValueError("Method preset development pages are missing from training_data")
manifest = {"schema_version": "piibench-split-v1", "scenario": scenario,
            "heldout_ids": [r["id"] for r in heldout], "development_ids": ids,
            "methods": {"gepa": {"developer_ids": ids,
                        "feedback_ids": split["feedback_record_ids"],
                        "pareto_ids": split["ranking_record_ids"]},
                        "webcloak": {"developer_ids": webcloak_ids},
                        "muzzle": {"developer_ids": muzzle_ids},
                        "autoguard": {"developer_ids": ids}}}
def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
save(work / "data" / scenario / "seeds.json", records + heldout)
save(work / "data" / scenario / "split.json", manifest)
save(work / "data/benchmark" / scenario / "tasks.json", tasks)
BIND

        config_json "$ROOT/configs/methods/gepa-$scenario.yaml" |
          jq --arg root "$WORK" --arg s "$scenario" '
            .seeds_json=($root+"/data/"+$s+"/seeds.json") |
            .split_manifest=($root+"/data/"+$s+"/split.json") |
            .html_dir=($root+"/data/"+$s+"/html") |
            .attack_prompts_file=($root+"/data/training_prompts.json") |
            .output_json=($root+"/gepa-"+$s+"/output.json") |
            .cache_path=($root+"/gepa-"+$s+"/cache.json") |
            .filter_cache_path=($root+"/gepa-"+$s+"/filter_cache.json") |
            .mutation_cache_path=($root+"/gepa-"+$s+"/mutation_cache.json")
          ' > "$configs/gepa-$scenario.json"

        config_json "$ROOT/configs/methods/webcloak-$scenario.yaml" |
          jq --arg root "$WORK" --arg s "$scenario" '
            .data.seeds=($root+"/data/"+$s+"/seeds.json") |
            .data.saved_split=($root+"/data/"+$s+"/split.json") |
            .data.html_dir=($root+"/data/"+$s+"/html") |
            .artifacts.search=($root+"/webcloak-"+$s+"/search.json") |
            .artifacts.prepared=($root+"/webcloak-"+$s+"/prepared.json") |
            .artifacts.evaluation=($root+"/webcloak-"+$s+"/evaluation.json")
          ' > "$configs/webcloak-$scenario.json"
        source="$ROOT/configs/training/muzzle-$scenario.json"
        config_json "$source" |
          jq --arg root "$WORK" --arg s "$scenario" '
            .data.seeds=($root+"/data/"+$s+"/seeds.json") |
            .data.saved_split=($root+"/data/"+$s+"/split.json") |
            .data.html_dir=($root+"/data/"+$s+"/html") |
            .output=($root+"/muzzle-"+$s) |
            .pi_agent.agent_pool_dir=($root+"/muzzle-agent-slots")
          ' > "$configs/muzzle-$scenario.json"
        jq --arg root "$WORK" --arg s "$scenario" '
          .search_output=($root+"/muzzle-"+$s+"/run-01/search") |
          .output=($root+"/muzzle-"+$s+"/run-01/confirmation")
        ' "$ROOT/configs/training/muzzle-$scenario-confirmation.json" > "$configs/muzzle-$scenario-confirmation.json"
        source="$ROOT/configs/methods/autoguard.json"
        [ "$scenario" != news ] || source="$ROOT/configs/methods/autoguard-news.json"
        jq --arg root "$WORK" --arg s "$scenario" '
          .output_dir=($root+"/autoguard-"+$s) |
          .cases_file=($root+"/data/autoguard/"+$s+"/training_cases.jsonl") |
          .data.binding_manifest=($root+"/data/autoguard/"+$s+"/binding_manifest.json") |
          .page_server.html_root=(if $s=="academic" then $root+"/data/academic/html"
                                 else $root+"/data/autoguard/news/news_html" end) |
          .limits.agent_slot_directory=($root+"/autoguard-agent-slots")
        ' "$source" > "$configs/autoguard-$scenario.json"

        pages="$WORK/data/benchmark/$scenario/pages.json"
    done

    # Keep the independent News comparison set available to GEPA's existing
    # selection command, with the original page/question bindings.
    base="$WORK/data/gepa/news/selection50"
    mkdir -p "$base"
    cp -R "$input/training_data/news/selection50/site" "$base/site"
    cp "$input/training_data/news/selection50/pages.json" "$base/pages.json"
    jq -n --slurpfile p "$base/pages.json" \
       --slurpfile q "$input/training_data/news/selection50/tasks.json" '
      ($p[0].pages|map({key:.page_id,value:.source_record})|from_entries) as $records |
      {schema_version:"piibench-tasks-v1",benchmark_id:$p[0].benchmark_id,scenario:"news",
       tasks:($q[0].tasks|map({case_id:(.page_id+"-selection"),page_id,task_type:"pii",question,
         metadata:{scenario:"news",question_id:.prompt_id,base_request_id,version,source_record_id:.page_id},
         reporter_info:$records[.page_id].reporter_info}))}
    ' > "$base/tasks.json"
    cp "$WORK/data/news/seeds.json" "$base/reserved_records.json"
    config_json "$ROOT/configs/methods/gepa-news-selection.yaml" |
      jq --arg root "$WORK" '
        .source=($root+"/gepa-news/output.json") |
        .pages=($root+"/data/gepa/news/selection50/pages.json") |
        .tasks=($root+"/data/gepa/news/selection50/tasks.json") |
        .split_manifest=($root+"/data/news/split.json") |
        .reserved_records=($root+"/data/gepa/news/selection50/reserved_records.json") |
        .output_dir=($root+"/gepa-news-selection")
      ' > "$configs/gepa-news-selection.json"

    jq --arg root "$WORK" '
      .output_dir=($root+"/piiguard-data") | .training_prompts=($root+"/data/training_prompts.json") |
      .scenarios |= with_entries(.key as $s | .value.source_seeds=($root+"/data/"+$s+"/seeds.json") |
        .value.saved_split=($root+"/data/"+$s+"/split.json") |
        if $s=="academic" then .value.html_dir=($root+"/data/academic/html") else . end) |
      .benchmark={academic:($root+"/data/benchmark/academic"),news:($root+"/data/benchmark/news")}
    ' "$ROOT/configs/methods/piiguard-data.json" > "$configs/piiguard-data.json"
    "$PYTHON" -m cli.piiguard prepare-data --config "$configs/piiguard-data.json"
    for scenario in academic news; do
        jq --arg root "$WORK" --arg s "$scenario" '
          .prepare_only=true | .output_dir=($root+"/piiguard-"+$s) |
          .training_prompts=($root+"/data/training_prompts.json") |
          .execution.domain_order=[$s] |
          .scenarios |= with_entries(select(.key==$s) |
            .value.optimizer_data=($root+"/piiguard-data/"+$s+"/optimizer_data.json") |
            .value.final_tasks=($root+"/data/benchmark/"+$s+"/tasks.json"))
        ' "$ROOT/configs/methods/piiguard-plan.json" > "$configs/piiguard-$scenario.json"
        "$PYTHON" -m cli.piiguard optimize --config "$configs/piiguard-$scenario.json"
    done
    for scenario in academic news; do
        base="$WORK/data/autoguard/$scenario/prompts"
        mkdir -p "$base"
        for name in discovery bandit; do
            jq -c --arg s "$scenario" --arg phase "$name" '.scenarios[$s][$phase][]' \
              "$ROOT/prompts/autoguard/training_questions.json" > "$base/${name}_prompts.jsonl"
        done
        "$PYTHON" -m cli.autoguard_pii bind-training --prompts "$base" \
          --source-config "$configs/gepa-$scenario.json" \
          --final-tasks "$WORK/data/benchmark/$scenario/tasks.json" \
          --base-url "$(jq -r '.page_server.base_url' "$configs/autoguard-$scenario.json")" \
          --output "$WORK/data/autoguard/$scenario" --page-profile "$scenario" |
          jq '{cases,unique_pages,model_calls,page_profile}'
    done
    printf '%s\n' "$input" > "$WORK/.training-ready"
    printf 'Prepared training inputs and initial candidates: %s/configs\n' "$WORK"
}

action=${1:---help}
[ "$#" -eq 0 ] || shift
case "$action" in
  --help|-h|help) help ;;
  prepare) prepare_training "$@" ;;
  select-gepa)
    [ "$#" -eq 0 ] || die "select-gepa takes no arguments; it selects GEPA News candidates"
    need_file "$WORK/.training-ready"
    config="$WORK/configs/gepa-news-selection.json"
    need_file "$config"
    selection_output=$(jq -r '.output_dir' "$config")
    if [ ! -f "$selection_output/plan.json" ]; then
        "$PYTHON" -m cli.gepa select prepare --config "$config"
    fi
    "$PYTHON" -m cli.gepa select run --config "$config"
    exec "$PYTHON" -m cli.gepa select report --config "$config"
    ;;
  run)
    [ "$#" -ge 1 ] || die "Expected method"
    method=$1; shift
    need_file "$WORK/.training-ready"
    [ "$#" -ge 1 ] || die "Expected academic or news"
    scenario=$1; shift
    scenario_arg "$scenario"
    config="$WORK/configs/$method-$scenario.json"
    need_file "$config"
    case "$method" in
      piiguard)
        [ "$#" -eq 0 ] || die "Unexpected extra arguments"
        jq '.prepare_only=false' "$config" > "$WORK/configs/piiguard-$scenario-run.json"
        exec "$PYTHON" -m cli.piiguard optimize --config "$WORK/configs/piiguard-$scenario-run.json"
        ;;
      gepa)
        [ "$#" -eq 0 ] || die "Unexpected extra arguments"
        exec "$PYTHON" -m cli.gepa --config "$config"
        ;;
      webcloak)
        [ "$#" -eq 0 ] || die "Unexpected extra arguments"
        exec "$PYTHON" -m cli.webcloak_pii --config "$config" optimize
        ;;
      muzzle)
        [ "$#" -eq 0 ] || die "MUZZLE run count comes from independent_runs in its JSON configuration"
        confirmation_template="$WORK/configs/muzzle-$scenario-confirmation.json"
        need_file "$confirmation_template"
        "$PYTHON" -c 'import sys; from defipi.config import load_config_file; from defipi.methods.muzzle.configuration import validate_confirmation; validate_confirmation(load_config_file(sys.argv[1]))' "$confirmation_template"
        "$PYTHON" -m cli.muzzle_pii validate --config "$config"
        run_count=$(jq -r '.independent_runs' "$config")
        batch_output=$(jq -r '.output' "$config")
        [ ! -e "$batch_output" ] || die "MUZZLE output already exists; choose a fresh output in $config"
        check_muzzle_runtime "$(jq -r '.pi_agent.container_image' "$config")"
        image=$(jq -r '.pi_agent.container_image' "$config")
        image_id=$(docker image inspect --format '{{.Id}}' "$image")
        mkdir -p "$batch_output"
        batch_exit=0
        run_records='[]'
        for ((run_index=1; run_index<=run_count; run_index++)); do
            printf 'MUZZLE %s: independent trial %s / %s\n' "$scenario" "$run_index" "$run_count"
            if run_muzzle_trial "$config" "$scenario" "$run_index" "$image_id" "$confirmation_template" "$batch_output"; then
                trial_exit=0
            else
                trial_exit=$?
                batch_exit=1
                case "$trial_exit" in 130|143) exit "$trial_exit" ;; esac
                printf 'MUZZLE trial %s failed (exit %s); continuing the configured independent trials\n' "$run_index" "$trial_exit" >&2
            fi
            run_records=$(jq -c --argjson index "$run_index" --argjson code "$trial_exit" \
              '. + [{run_index:$index,exit_code:$code}]' <<< "$run_records")
            jq -n --argjson count "$run_count" --argjson runs "$run_records" '
              {independent_runs:$count,runs:$runs,
               status:(if ($runs|length)<$count then "running"
                       elif any($runs[]; .exit_code!=0) then "completed_with_failed_runs"
                       else "completed" end)}' > "$batch_output/completion.json"
        done
        exit "$batch_exit"
        ;;
      autoguard)
        need_file "$WORK/data/autoguard/$scenario/training_cases.jsonl"
        need_file "$WORK/data/autoguard/$scenario/binding_manifest.json"
        [ "$#" -eq 1 ] && [ "$1" = --authorize-paid-calls ] || die "AutoGuard requires --authorize-paid-calls, as in its native CLI"
        exec "$PYTHON" -m cli.autoguard_pii search --config "$config" --authorize-paid-calls
        ;;
      *) die "Expected gepa, piiguard, webcloak, muzzle or autoguard" ;;
    esac
    ;;
  *) help >&2; die "Unknown command: $action" ;;
esac
