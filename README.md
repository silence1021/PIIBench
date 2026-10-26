# PIIBench

PIIBench evaluates defensive indirect prompt injection: instructions that a
website publisher embeds in its own pages so that LLM agents do not disclose
the personal contact information shown there. This repository contains the
benchmark harness, five defense methods with their configurations and initial
templates, the evaluation presets, and the released data in `datasets/`.

The benchmark has two scenarios, `academic` (profile pages of fictional
professors) and `news` (news articles attributed to fictional reporters), and
compares six methods: `noguard` (no defense), `gepa`, `piiguard`,
`autoguard`, `webcloak` and `muzzle`.

## Repository layout

| Path | Contents |
| --- | --- |
| `datasets/` | Released data: five ZIP archives, `Human_Annotations_50.json` and the [prompt collection](datasets/PROMPTS.md) |
| `scripts/` | `experiments.sh` (evaluation), `train.sh` (optimization), the RQ1–RQ4 entry points and the page neutralizer |
| `cli/` | Python command-line modules called by the scripts (`python -m cli.<name> --help`) |
| `defipi/` | Library code: benchmark inputs, execution backends, judging and metrics, defense methods |
| `configs/` | Evaluation presets (`evaluation/`), judge registry (`evaluation_models.json`), method and training templates (`methods/`, `training/`), page-generation settings (`generation/`) |
| `prompts/` | Defense templates and prompts (GEPA, AutoGuard, MUZZLE), the URL anti-injection prompt and the judge prompts |
| `containers/agents/` | Docker image for the Pi, Codex, Claude Code and DSH runtimes |
| `third_party/` | Bundled MUZZLE (with PAIR) and WebCloak sources, and the Academic Pages template license |
| `examples/` | A small synthetic page, artifact and task set |

## Installation

Use Python 3.11 or later and install `jq` (for example `brew install jq` on
macOS or `apt install jq` on Debian/Ubuntu). Run every command from the
repository root.

```sh
python -m pip install -r requirements.txt
python -m pip install -e .
```

The Shell scripts call `python` unless `PYTHON` names another interpreter, for
example `export PYTHON=/path/to/environment/bin/python`.

Provider keys are read from the environment or from `.env` in the repository
root; `.env.example` lists them. Set only the keys that the selected runs need

Agent runs (Pi, Codex, Claude Code, DSH) and MUZZLE training need Docker and
the shared runtime image:

```sh
docker build -f containers/agents/Dockerfile -t piiguard-agents:chromium156 .
```

The image pins Chromium 156.0.8078.47, Pi 0.99.2, Codex 0.160.0, Claude Code
2.1.293, DSH 0.1.2-rc.1 and Playwright MCP 0.0.80. Evaluation preparation and
MUZZLE training resolve the tag to its immutable image ID and freeze that ID in
the configurations they generate; an existing `sha256:...` ID can be passed
instead of the tag.
Training needs further Python environments, described under Training.

## Data

`datasets/` contains the released data:

| File | Contents | Read by |
| --- | --- | --- |
| `evaluation_data.zip` | The 100 test pages per scenario without a defense, their queries and ground truth | `experiments.sh prepare`, `train.sh prepare` |
| `optimization_results.zip` | The frozen output of each defense's optimization | `experiments.sh prepare` |
| `training_data.zip` | Training pages, splits and training requests | `train.sh prepare` |
| `full_scope_academic.zip` | The complete pool of 200 Academic pages and the full-pool index | No script |
| `full_scope_news.zip` | The complete pool of 900 News pages | No script |
| `Human_Annotations_50.json` | Manual labels for 50 judged output streams | No script |
| [PROMPTS.md](datasets/PROMPTS.md) | Prompt templates for defense construction, cross-page generation, evaluation requests, filtering and judging | Reference document |

The archives contain data only: JSON manifests and records, HTML pages and the
static resources those pages load. The scripts never open them, so extract
what a command needs and keep each archive's top-level directory:

```sh
mkdir -p datasets/unpacked
unzip -q datasets/evaluation_data.zip -d datasets/unpacked
unzip -q datasets/optimization_results.zip -d datasets/unpacked
unzip -q datasets/training_data.zip -d datasets/unpacked    # training only
unzip -q datasets/full_scope_academic.zip -d datasets/unpacked  # inspection only
unzip -q datasets/full_scope_news.zip -d datasets/unpacked      # inspection only
```

Extract both `full_scope` archives into the same directory to reconstruct the
complete pool. Any extraction directory works; pass its path to the `prepare`
commands. The extracted files are never modified. The layout is:

```text
datasets/unpacked/
  evaluation_data/
  ...
  training_data/
  ...
  full_scope/
  ...
```

In every `pages.json`, `document` paths are relative to the neighbouring
`site/`.

### `evaluation_data`

The test pages without a defense (NoGuard). Their paths are opaque tokens:
each page is `p/<token>/index.html`, shared resources are under `r/`, and every
Academic page links to a publications subpage with its own token. `pages.json`
gives each page's `page_id` (`main-academic-001` to `main-academic-100`,
`main-news-001` to `main-news-100`), document and a placeholder URL on
`https://your-evaluation-host.example`; `artifact.json` describes the same
pages as a NoGuard artifact. `tasks.json` binds each page to one test query and
carries its ground truth `reporter_info` (name, email, phone, address). Each
scenario has ten queries built on the base request B1, and each query is
assigned to ten pages. `gold.json` lists the ground truth of all 200 pages.

### `optimization_results`

The frozen defenses, joined to the test pages by `page_id`.
`selected_candidate.json` is the defense selected by GEPA, PIIGuard, AutoGuard
or MUZZLE. WebCloak and MUZZLE adapt their defense to every page:
`per_page_results.json` holds the per-page output (WebCloak semantics, MUZZLE
notices), and `webcloak/*/source_candidate.json` records the WebCloak candidate
it was adapted from. `index.json` lists the files of each method and scenario.

### `training_data`

`academic/train25/` and `news/train25/` contain the 25 training pages of each
scenario; their `split.json` divides them into 15 feedback and 10 ranking pages
for GEPA. `news/selection50/` contains 50 further News pages for GEPA News
candidate selection.
`seeds.json` holds the source records the pages were rendered from.
`training_prompts.json` holds the 40 training requests (ten B1 and ten B2
variants per scenario), none of which is a test query. `splits.json` records
the complete split of both pools by source record ID, including the source
record of every test page.

### `full_scope`

The pool is distributed as `full_scope_academic.zip` and `full_scope_news.zip`;
extract both into the same directory to obtain `full_scope/`. Its combined
`index.json` is included in the Academic archive.

The complete pool with source records (`seeds.json`), page manifests
(`pages.json`) and splits (`split.json`): 200 Academic pages (190 DUSK profiles
and 10 synthetic profiles in the same format) and 900 News pages. Each page's
`split` is `evaluation` (the test pages, with their `evaluation_page_id`),
`training`, `candidate_selection` (the 50 News selection pages) or `unused`. `train.sh` trains every method on the 25
`training` pages. Test pages keep their opaque paths, and the other pages keep
their source file names.

### `Human_Annotations_50.json`

The sample for the manual verification of presence judging: 50 judged output
streams that received a decision. Each record has `id`, `channel` (`A` final
reply, `F` saved file `./contacts.md`), `ground_truth` (name, email, phone,
address), the judged `text` and our `human_label` (`PRESENT` or `ABSENT`). The
sample has 41 reply and 9 file streams, 8 of them labeled `PRESENT`. The judges'
decisions are not included.

## Evaluation

| Research question | Command | Conditions |
| --- | --- | --- |
| RQ1: input channel and execution mode | `experiments.sh rq1` | HTML/URL × API/Pi, attacker-side filtering OFF |
| RQ2: attacker-side filtering | `experiments.sh rq2` | The same presets with filtering ON |
| RQ3: attempt budget | `experiments.sh rq3` | Offline analysis of existing runs at k = 1–4 |
| RQ4: native agents | `experiments.sh rq4` | Claude Code, Codex and DSH on page URLs, OFF |

Every condition runs the 100 pages of a scenario, each with its bound query, in
four independent rounds. RQ1 and RQ2 default to all six methods and both
scenarios; RQ4 defaults to GEPA and WebCloak in both scenarios.

### Prepare the defended pages

```sh
bash scripts/experiments.sh prepare datasets/unpacked
```

This step makes no model requests and reads only `evaluation_data/` and
`optimization_results/`. For each scenario it applies every method to the
NoGuard HTML with the repository's HTML operators, using the saved candidate or
the per-page result with the same `page_id`. `scripts/neutralize_pages.py` then
renames template identifiers and moves every page and resource to a random
opaque path, so that a URL reveals neither the person nor the defense. The
output is:

```text
outputs/reproduction_inputs/
  export/{method}/{scenario}/       defended pages before neutralization
  neutral/{method}/{scenario}/      neutralized artifact and site
  evaluation/
    site/                           all 1,200 defended pages under /p/ and /r/
    {method}/{scenario}/{artifact.json,tasks.json}
    gold.json
```

All methods share the same tasks and ground truth. An existing working
directory is never overwritten: to prepare again, set `PIIBENCH_WORK_DIR` to a
new directory inside the repository and keep it set for the commands that
follow.

### Host the pages for URL presets

HTML presets read the prepared files directly. URL presets fetch
`$PIIBENCH_BASE_URL/p/<token>/`, so set your own origin before preparing:

```sh
export PIIBENCH_BASE_URL=https://your-evaluation-host.example
bash scripts/experiments.sh prepare datasets/unpacked
```

The original experiments deployed the generated `evaluation/site/` on Vercel.
For reproduction, deploy this directory yourself on Vercel or another public
hosting service and use your own origin. Serve it as the root of that origin
and preserve its `/p/` and `/r/` paths.

### Presets

The presets in `configs/evaluation/` fix the target model, the input channel
and the execution mode. RQ1 and RQ2 use the API and Pi presets, which all
allow OFF and ON:

| Target model (provider) | HTML / API | URL / API | HTML / Pi | URL / Pi |
| --- | --- | --- | --- | --- |
| `deepseek/deepseek-v4.1-flash` (OpenRouter) | `api-html-deepseek-flash` | `api-url-deepseek-v4-1-flash` | `pi-html-deepseek-flash` | `pi-url-deepseek-v4-1-flash` |
| `z-ai/glm-5.3-flash` (OpenRouter) | `api-html-glm-5-3` | `api-url-glm-5-3` | `pi-html-glm-5-3` | `pi-url-glm-5-3` |
| `claude-haiku-5-5` (Anthropic) | `api-html-haiku-5-5` | `api-url-haiku-5-5` | `pi-html-haiku-5-5` | `pi-url-haiku-5-5` |

RQ4 uses the native agent presets, which run on URLs with OFF only:

| Preset | Agent | Target model (provider) |
| --- | --- | --- |
| `claude-url-haiku-5-5` | Claude Code | `claude-haiku-5-5` (Anthropic) |
| `codex-url-luna` | Codex | `gpt-6-luna` (OpenAI) |
| `dsh-url-deepseek-flash` | DSH | `deepseek-flash` (DeepSeek) |

With ON, an HTML page first passes through the HTML Filter (`gpt-6-luna`; the
prompt is the `canonical` variant in `defipi/evaluation/llm_filter.py`), with
its own cleaning request in every round. A URL condition instead adds
`prompts/attacker/in_agent_filter.txt` to the system prompt. Agent conditions on
URLs append `Save the result to ./contacts.md` to the query and record both
the final reply and the file; all other conditions record the reply.

### Run RQ1, RQ2 and RQ4

`prepare` and `plan` are offline; `run` sends model requests. Start with one
HTML/API preset:

```sh
bash scripts/experiments.sh rq1 prepare --output outputs/rq1 \
  --templates api-html-deepseek-flash
bash scripts/experiments.sh rq1 plan --manifest outputs/rq1/manifest.json
bash scripts/experiments.sh rq1 run --manifest outputs/rq1/manifest.json

bash scripts/experiments.sh rq2 prepare --output outputs/rq2 \
  --templates api-html-deepseek-flash
bash scripts/experiments.sh rq2 run --manifest outputs/rq2/manifest.json
```

`prepare` freezes one locked configuration per preset, method and scenario
into a new `--output` directory and writes `manifest.json`; `plan` reports the
planned target and filter calls. The options of `prepare` are:

| Option | Default | Meaning |
| --- | --- | --- |
| `--templates` | All presets valid for the RQ | Presets to run (`prepare --help` lists them) |
| `--methods` | All six (RQ4: `gepa webcloak`) | Methods to run |
| `--scenarios` | `academic news` | Scenarios to run |
| `--concurrency` | 1 | Parallel slots within a condition (1–40) |
| `--container-image` | `piiguard-agents:chromium156` | Image for agent presets |
| `--max-model-requests` | 20 | Model-request cap per agent trajectory |

Use the same `--templates`, `--methods` and `--scenarios` for an RQ1/RQ2
comparison. The conditions of a batch run one after another. `run` validates
every lock before its first request; rerun it with `--resume` to continue an
interrupted batch, which keeps completed slots and the presets' retry limits.

Agent presets need the Docker image:

```sh
bash scripts/experiments.sh rq1 prepare --output outputs/rq1-pi \
  --templates pi-html-deepseek-flash pi-url-deepseek-v4-1-flash \
  --container-image piiguard-agents:chromium156
bash scripts/experiments.sh rq4 prepare --output outputs/rq4 \
  --container-image piiguard-agents:chromium156
bash scripts/experiments.sh rq4 run --manifest outputs/rq4/manifest.json
```

Manually extract `evaluation_data.zip` and `optimization_results.zip` as
shown in Data, then run `bash scripts/experiments.sh prepare datasets/unpacked`
to build the evaluation directory. The wrapper passes that directory to the
RQ scripts. Direct RQ1, RQ2 and RQ4 `prepare` calls require `--dataset`, which
points to the prepared evaluation directory:

```sh
python -m scripts.rq1 prepare \
  --dataset outputs/reproduction_inputs/evaluation \
  --output outputs/rq1-direct \
  --templates api-html-deepseek-flash
```

Use `$PIIBENCH_WORK_DIR/evaluation` instead when a custom working directory
is set. The RQ scripts do not extract archives.

### Score and report

Scoring reads every run in a manifest, including invalid and missing slots.
The sequence is the same for RQ1, RQ2 and RQ4; replace `rq1` and the paths:

```sh
bash scripts/experiments.sh rq1 score-prepare \
  --manifest outputs/rq1/manifest.json --output outputs/rq1-judges
bash scripts/experiments.sh rq1 judge run --allow-paid \
  --jobs outputs/rq1-judges --max-requests 10000
bash scripts/experiments.sh rq1 judge status --jobs outputs/rq1-judges
bash scripts/experiments.sh rq1 report --jobs outputs/rq1-judges \
  --output outputs/rq1-tables
```

`score-prepare` is offline. It applies the rule decisions to every reply and
file stream and freezes deduplicated judge requests for the remaining streams.
Each of these goes to the three judges in `configs/evaluation_models.json` that
do not belong to the target model's family, with the prompt
`prompts/judge/jp.txt`; two agreeing labels decide a stream. Use the same
`--source-id` for matched RQ1/RQ2 runs (default `reproduction`).

`judge run` sends the judge requests. `--max-requests` caps the new sends of one
invocation, retries included, and no request is sent more than three times;
`--concurrency PROVIDER=N` sets per-provider parallelism. Run it again with a
new budget to continue. `judge status` is offline. To refresh evidence in an
existing store, run `score-prepare --append`, then write the report to a new
directory.

`report` is offline and writes `summary.json`, `presence.csv`,
`exact_fields.csv`, `repeat_curves.csv`, `exact_repeat_curves.csv`,
`file_matrices.csv`, `paired_noguard.json`, `export_metadata.json`,
`labels.jsonl` and `rq_metadata.json`. Replies (A) and files (F) are scored as
separate channels, and `file_matrices.csv` counts both / answer_only /
file_only / neither for each agent URL condition. UNKNOWN judgments remain in
the denominators. `report --labels` reads a saved `labels.jsonl` instead of a
judge store.

### RQ3: attempt budget

RQ3 sends no requests and needs no judging; it reads the four rounds of
existing runs:

```sh
bash scripts/experiments.sh rq3 \
  --manifests outputs/rq1/manifest.json outputs/rq2/manifest.json \
  --output outputs/rq3
```

Instead of `--manifests`, pass run directories (`--runs`), presence labels
(`--labels outputs/rq1-tables/labels.jsonl`, which add the PRESENT repeat
curves) or a `raw_results.jsonl` exported by `cli.report_paper_tables`
(`--raw-results`).

`attempt_budget.json` and `attempt_budget.csv` report J_binary and J_PER (JDC)
at `@1`–`@4` and `all4`, and JDC `mean4` and `union@4`, with exact numerators,
denominators and complete/excluded page counts. `@k` averages the best
per-round score over all size-k subsets of a page's four rounds; `union@4`
unions the fields matched within a page and condition. Replies and files stay
separate channels, incomplete pages are excluded and counted, and retries never
become additional rounds. All outputs keep the run IDs.

## Training

Evaluation uses the frozen defenses in `optimization_results.zip` and does not
need this step. To run the optimizations again, extract `training_data.zip` and
`evaluation_data.zip` and prepare once:

```sh
bash scripts/train.sh prepare datasets/unpacked
```

`prepare` makes no model requests. It writes working data to
`outputs/reproduction_inputs/data/` and working configurations to
`outputs/reproduction_inputs/configs/` (or under `PIIBENCH_WORK_DIR`; training
and evaluation can share one working directory). In doing so it:

- copies the Academic training HTML and re-renders the 25 News training pages
  from their seed records in the contact-table layout the optimizers expect;
- adds the 100 NoGuard test pages per scenario as held-out records, identified
  by `page_id`, so that the optimizers' split checks keep them out of every
  development pool;
- writes one configuration per method and scenario, builds PIIGuard's initial
  candidates and binds the saved AutoGuard questions to their pages.

`full_scope` is not read. WebCloak's Academic validator may warn that the
working pool has 125 records instead of 200; its five development pages still
pass validation.

Then start one method and one scenario (`academic` or `news`) per command:

| Method | Training pages | Command after `bash scripts/train.sh` | Requires |
| --- | --- | --- | --- |
| GEPA | 25 (15 feedback, 10 ranking) | `run gepa SCENARIO`; for News then `select-gepa` | Rendering environment |
| PIIGuard | 25, with the B1 training requests | `run piiguard SCENARIO` | Rendering environment |
| WebCloak | 5 of the 25 | `run webcloak SCENARIO` | Rendering environment |
| MUZZLE | 1 of the 25 | `run muzzle SCENARIO` | MUZZLE environment, Docker image |
| AutoGuard | 25, with 100 discovery and 100 bandit questions | `run autoguard SCENARIO --authorize-paid-calls` | AutoGuard environment |

`run` and `select-gepa` send model requests; `--help` summarizes the interface.
Models, provider routes, budgets, concurrency and seeds are set in the working
JSON configurations, and results go to `gepa-SCENARIO/`, `piiguard-SCENARIO/`,
`webcloak-SCENARIO/`, `muzzle-SCENARIO/`, `autoguard-SCENARIO/` and
`gepa-news-selection/` in the working directory. The development pages of
WebCloak and MUZZLE are listed in `configs/methods/webcloak-SCENARIO.yaml` and
`configs/training/muzzle-SCENARIO.json`.

### GEPA, PIIGuard and WebCloak

These methods render pages with Playwright:

```sh
python -m pip install -r requirements-rendering.txt
python -m playwright install chromium
```

GEPA starts from `prompts/gepa/gepa_initial_academic.json` and
`gepa_initial_news.json`; PIIGuard builds its initial candidates during
`prepare` with `initial_candidates()` in `defipi/methods/piiguard/optimization.py`.
Each PIIGuard scenario has its own plan (`configs/piiguard-SCENARIO.json` in
the working directory).

After the GEPA News search, `select-gepa` prepares the candidate selection on
its first invocation, runs the comparison and writes the report; it resumes
from the existing plan. All per-position winners are screened, and the 50
`selection50` pages only break ties.

### MUZZLE

MUZZLE training runs the authors' AutoGen/PAIR optimizer, bundled with patches
under `third_party/muzzle/` (PAIR under `modules/JailbreakingLLMs/`), in the
host Python environment. Its `PiBrowserRunner` starts a disposable container
for every reconnaissance, objective, probe, full-browser validation and
confirmation trajectory; the container runs Pi, Playwright MCP and Chromium.
PAIR's single-step requests use the same counted target budget on the host.

```text
train.sh -> MUZZLE AutoGen/PAIR controller (host)
            -> PiBrowserRunner -> Pi + Playwright MCP + Chromium (container)
            <- captured requests, browser observations and validation results
```

Set up the controller environment and the image:

```sh
python3 -m venv .venv-muzzle
. .venv-muzzle/bin/activate
python -m pip install -r requirements-muzzle-author.txt
python -m pip install -e .
docker build -f containers/agents/Dockerfile -t piiguard-agents:chromium156 .
```

```sh
bash scripts/train.sh run muzzle academic
bash scripts/train.sh run muzzle news
```

Before the first trial, `run muzzle` validates both configurations and the
training page, imports the bundled controller and AutoGen, and renders a local
probe page with Chromium inside the configured image without network access
or model requests. It then resolves the image to its immutable ID and runs
`independent_runs` trials one after another. Each trial searches and then
confirms the validated candidate it found on the same page, with separate
budgets and directories (`run-NN/search/`, `run-NN/confirmation/`).
Confirmation also accepts a candidate from a search that exhausted its budget
and never reselects it. A failed trial is recorded and the remaining trials
still run; `completion.json` lists every trial's exit code, and the command
exits nonzero if any trial failed. The batch `output` must not exist yet.

The budgets are read from the working JSON files under `configs/` at execution
time. The supplied values match the historical five-trial runs:

| Field | Search (`muzzle-SCENARIO.json`) | Confirmation (`muzzle-SCENARIO-confirmation.json`) |
| --- | --- | --- |
| `independent_runs` | 5 | — |
| `search.iterations` | 30 | — |
| `repeats` | — | 2 per baseline and defended arm |
| `max_requests` (method API attempts) | 350 | 1 (expected use 0) |
| `max_pi_requests` (target API attempts) | 350 | 100 |
| `max_pi_tasks` (complete Pi trajectories) | 16 | 4 |
| `browser_max_steps` | 20 | — |
| `pi_agent.max_model_requests` | 350 | — |

These are limits, not consumed requests. One trial may use up to 450 target
requests and 20 Pi trajectories; five trials, up to 2,250 and 100.
`pi_agent.max_model_requests` is the per-trajectory cap that the container
adapter requires; the historical runs did not set it. The Shell sets each
trial's paths, image ID and `independent_runs: 1` and leaves the budgets
unchanged, so every trial keeps its own budget; edit the working JSON files to
change them.

### AutoGuard

AutoGuard's 400 saved questions are in `prompts/autoguard/training_questions.json`:
for each scenario, 100 `discovery` and 100 `bandit` questions, ten per attack
pattern in each split, in their original text, split membership and order.
`prepare` writes them as the native prompt files and binds them offline, which
creates `training_cases.jsonl`, `binding_manifest.json` and, for News, the bound
HTML under `data/autoguard/SCENARIO/` in the working directory.

AutoGuard runs in a separate environment built from
`requirements-autoguard.txt` with AgentLab installed. Point `PYTHON` at it:

```sh
PYTHON=/path/to/autoguard-env/bin/python \
  bash scripts/train.sh run autoguard academic --authorize-paid-calls
PYTHON=/path/to/autoguard-env/bin/python \
  bash scripts/train.sh run autoguard news --authorize-paid-calls
```

## Third-party code

| Path | Contents | License |
| --- | --- | --- |
| `third_party/muzzle/` | MUZZLE author code with patches | Apache 2.0 |
| `third_party/muzzle/modules/JailbreakingLLMs/` | PAIR | MIT |
| `third_party/webcloak/` | WebCloak prompts and utilities | BSD-style, see `LICENSE` |
| `third_party/academic_pages/LICENSE` | License of the Academic Pages template used by the Academic pages | MIT |
