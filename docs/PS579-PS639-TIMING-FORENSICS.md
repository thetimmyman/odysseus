# PS-579 PS-639 timing forensics

This is a read-only forensic analysis of the sealed PS-639 RTX4500 G0/G1/G2
block. The sealed evidence was not rewritten and no runtime semantics changed.

## Source evidence

| arm | run | total wall | attempt | model rounds | verifier | evidence package |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| G0 | `ps639-compose-drift-20260915T141520Z` | 199.514s | 197.902s | 106.133s + 91.769s | 1.202s | `beeb221714dd595724f9bbcf504de4c87dbc6780679788df6638f5f0daa816ba` |
| G1 | `ps639-compose-drift-20260915T141847Z` | 205.999s | 204.628s | 111.588s + 93.039s | 1.248s | `81f3c085ce8996b2cd9640f174e93492b65894afc790a41fa6b038367854a026` |
| G2 | `ps639-compose-drift-20260915T142220Z` | 349.998s | 348.263s | 176.920s + 171.343s | 1.216s | `c543ff67912b3d6d2f098105050abc373483943d8e86a85a8bfb9e39035962bb` |

The attempt ledger records two rounds for every arm, with 3,216 and 2,953
evaluation tokens respectively. The attempt elapsed time equals the sum of the
two recorded round times for each arm. G2 therefore has no hidden additional
model call: its extra 150.361s versus G0 is 70.787s in round 1 and 79.574s in
round 2.

## Timestamped timeline

| phase | G0 | G1 | G2 | coverage |
| --- | --- | --- | --- | --- |
| capability receipt observed | 14:15:15.936677Z | 14:18:43.141936Z | 14:20:43.129965Z | sealed routing inputs |
| routing decision | 14:15:20.730749Z | 14:18:47.417351Z | 14:22:21.052268Z | sealed routing decision |
| attempt ledger event | 14:18:38.744892Z | 14:22:12.148848Z | 14:28:09.422446Z | start is not separately sealed; event follows request setup |
| model round 1 | 106.133s; TTFT 13.407s | 111.588s; TTFT 18.716s | 176.920s; TTFT 84.904s | Ollama client timing |
| model round 2 | 91.769s; TTFT 91.767s | 93.039s; TTFT 93.038s | 171.343s; TTFT 171.341s | Ollama client timing |
| verifier | 14:18:38.753204–14:18:39.955784Z | 14:22:12.154131–14:22:13.402799Z | 14:28:09.429109–14:28:10.645415Z | sealed VerificationReceipt |
| evidence sealed | 14:18:40.483050Z | 14:22:13.652415Z | 14:28:11.287672Z | sealed EvidencePackage |

## Accounting

The strongest directly measured accounting is:

```text
G0 199.514 = 197.902 model/API + 1.202 verifier + 0.410 residual
G1 205.999 = 204.628 model/API + 1.248 verifier + 0.123 residual
G2 349.998 = 348.263 model/API + 1.216 verifier + 0.519 residual
```

The residual includes packet/context preparation, route finalization, endpoint
resolution, write application not separately timestamped from the request,
deterministic loop bookkeeping, evidence construction/validation, persistence,
and process/SSH lifecycle. The artifact format does not provide separate
timestamps for those phases, so they are not retrospectively split.

Receipt lookup/refresh is also not a timed phase in the sealed run. The selected
receipt was already observed 97.923s before the G2 routing decision, had the
same material identity, and `receipt_ref_matches_store=true`. G2 did not cross
the approximately 300s liveness window during its own run and no receipt
regeneration or capability rediscovery appears in its evidence.

## Hypothesis disposition

* **H1, freshness boundary:** not supported. G2 used a fresh, valid receipt;
  no in-run refresh or regeneration is recorded.
* **H2, execution order:** remains a possible contributor to the API latency,
  but historical thermal/load/GPU/queue/network snapshots were not captured and
  cannot be reconstructed from these artifacts.
* **H3, model/API latency:** supported and directly measured. G2 was slower in
  both Ollama rounds, including TTFT (84.904s versus 13.407s/18.716s in round 1).
* **H4, G2 deterministic path:** not supported. G2 made zero planner calls and
  its verifier/evidence residual was only 0.109s above G0.
* **H5, evidence/store overhead:** not supported. Evidence and verifier account
  for only 1.735s of G2 and the unexplained residual is 0.519s.

## Classification

The original +150s observation is classified as **runtime/model-request latency
variance observed in the sequential order**, not G2 semantic or orchestration
overhead. The evidence explains essentially all of the excess wall time at the
stage level, but does not establish why the Ollama request was slower. A balanced
repeat is not required to diagnose the location of the delay; it remains the
smallest next experiment if PS-579 later needs to separate execution-order
effects from runtime variance.
