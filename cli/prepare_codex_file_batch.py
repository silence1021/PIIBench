"""Prepare the four Codex/Luna medium URL file-task conditions without inference."""
import argparse
import json
from defipi.configuration.agent_file_batch import CONDITIONS, DELIVERY_CLAUSE, file_tasks
from defipi.configuration.agent_file_batch import prepare_batch as prepare_file_batch


def prepare_batch(dataset, output_dir, container_image, *, concurrency=40):
    return prepare_file_batch(dataset, output_dir, container_image,
                              template="codex-url-luna", run_prefix="codex-api-file",
                              concurrency=concurrency)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, help="Extracted released dataset root")
    parser.add_argument("--output-dir", required=True, help="New file-task output directory")
    parser.add_argument("--container-image", required=True)
    parser.add_argument("--concurrency", type=int, default=40)
    args = parser.parse_args(argv)
    try:
        manifest = prepare_batch(args.dataset, args.output_dir, args.container_image,
                                 concurrency=args.concurrency)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
