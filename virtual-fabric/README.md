# virtual-fabric

Build a virtual SONiC data center (spines, leaves and hosts) on one machine, then watch it in the dashboard.
Start it from the repo root with `./run.sh virtual`; see the [main README](../README.md).

| File | What it does |
|---|---|
| `fabgen.py` | Designs the fabric: writes the containerlab topology, `fabric.json`, the BGP/ECMP config script and the Prometheus config into `build/` |
| `rebuild-fabric.sh` | Rebuilds a running fabric at a new size; the Command Center's `/api/fabric/apply` calls it |
