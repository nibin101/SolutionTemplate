# Sample captures

Drop JPEGs here and the `simulator` capture source replays them in order instead
of generating synthetic frames — useful for a demo that should look like real
clinical photography.

Each replayed frame is stamped with a shot number and timestamp before it is
sent, so every one has unique bytes and the content-hash deduplication does not
(correctly) discard the demo.

**Do not commit real patient photographs.** Use stock or consented images only.

Leave this folder empty to get the generated placeholder series instead.
