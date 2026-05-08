# Architecture diagrams

PlantUML sources + rendered PNGs for the architecture section of
the dissertation. Re-render with:

```bash
# Recommended: official Docker image
docker run --rm -v "$PWD/docs/architecture:/work" -w /work \
    plantuml/plantuml:1.2024.7 -tpng "*.puml"

# Or with a local jar:
curl -fsSLo /tmp/plantuml.jar \
    https://github.com/plantuml/plantuml/releases/download/v1.2024.7/plantuml-1.2024.7.jar
java -jar /tmp/plantuml.jar -tpng docs/architecture/*.puml
```

| Source                              | Rendered PNG                            | Reference         |
|-------------------------------------|-----------------------------------------|-------------------|
| `c4-context.puml`                   | `C4-Context.png`                        | Рисунок 5         |
| `c4-containers.puml`                | `C4-Containers.png`                     | Рисунок 6         |
| `c4-components-recommendation.puml` | `C4-Components-Recommendation.png`      | Рисунок 7         |
| `idef0-a0.puml`                     | `IDEF0-A0.png`                          | Рисунок 1 (A-0)   |
| `idef0-a1-decomposition.puml`       | `IDEF0-A1.png`                          | Рисунок 3 (A1)    |
| `idef0-a3-decomposition.puml`       | `IDEF0-A3.png`                          | Рисунок 4 (A3)    |
| `bpmn-cashback-flow.puml`           | `BPMN-Cashback-Flow.png`                | Приложение №1     |
| `uml-classes.puml`                  | `UML-Classes.png`                       | Глава 2.2         |
