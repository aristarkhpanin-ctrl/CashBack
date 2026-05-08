# Screenshots

These PNGs are referenced from the master `README.md`. They are
**captured manually** after the first `make up` (Claude Code can't drive
a browser).

## What to capture

After `make up && make seed-users && make seed-history && make
trigger-etl && make seed-recommendations && make train-models`, take
the five screenshots listed below and save them under the exact
filenames so the relative links in the README resolve.

| Filename                          | URL                                            | What to capture                                              |
|-----------------------------------|------------------------------------------------|--------------------------------------------------------------|
| `01_dashboard.png`                | http://localhost:3000/                         | KPI tiles + segment heatmap + top-5 ROI table                |
| `02_campaigns_wizard.png`         | http://localhost:3000/campaigns                | Step 2 (Целевая аудитория) — Audience preview shows real numbers |
| `03_analytics_funnel.png`         | http://localhost:3000/analytics                | Funnel chart + Segment × MCC matrix                          |
| `04_experiments.png`              | http://localhost:3000/experiments              | Experiments list **or** the z-test calculator tab            |
| `05_mlflow.png`                   | http://localhost:5000/                         | Experiment list with the LightGBM run + a `Production` tag    |

## Tips

* Use a 1440×900 viewport for consistent screenshots.
* Crop to remove the browser chrome so the README looks tidy.
* PNG (lossless) is preferred over JPEG — the dashboards have crisp
  text + colour gradients in the heat-map cells.
* The repository's `.gitignore` does **not** exclude PNGs in this
  folder, so committing them is fine.
