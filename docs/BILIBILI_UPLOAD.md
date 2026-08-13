# Bilibili Upload Adapter

The repository includes the upload orchestration contract, but not account
credentials or a publishing queue.

## Contract

`scripts/upload_video.py` expects a local adapter directory configured as:

```yaml
paths:
  biliup_root: ~/biliup-data
```

The adapter must provide:

```text
~/biliup-data/pipeline/
├── bin/enqueue-video.sh
└── bin/publish-queue.sh
```

`enqueue-video.sh` must accept the following arguments:

```text
--video PATH
--title TEXT
--desc TEXT
--dynamic TEXT
--tag TEXT
--job-name NAME
--public            # optional flag
```

`publish-queue.sh` must process the queue and leave a completed job directory
under `pipeline/done/`. Failed jobs should be moved under `pipeline/failed/`.

## Safety Gates

Before invoking the adapter, the workflow checks:

1. The project manifest is `validated` or `success`.
2. The final video SHA256 matches the manifest.
3. A deterministic job name is derived from the project name and video hash.
4. A local lock prevents concurrent pipeline/upload operations.
5. A success marker records the hash, visibility, job name, and done directory.

Running the same upload command for the same video is idempotent when the
adapter's done directory still exists.

## Usage

```bash
python3 scripts/upload_video.py \
  --project projects/my-topic \
  --video projects/my-topic/final_video.mp4 \
  --name my-topic \
  --style-plan projects/my-topic/STYLE_PLAN.yaml \
  --style-history STYLE_HISTORY.md \
  --title '示例标题'
```

Add `--public` only when you explicitly want public visibility. Credentials,
cookies, queue files, and upload markers must remain local and ignored.
