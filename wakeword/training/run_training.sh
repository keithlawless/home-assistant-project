#!/bin/sh
# Wait for data prep, then generate clips, augment, and train.
cd "$(dirname "$0")"
while ! grep -q "prep done" prep.log; do
  if ! pgrep -f prep_data.py >/dev/null; then echo "prep_data.py stopped without finishing"; exit 1; fi
  sleep 20
done
for step in generate_clips augment_clips train_model; do
  echo "=== $step $(date)"
  .venv/bin/python openwakeword/openwakeword/train.py --training_config hey_taco.yaml --$step || { echo "FAILED at $step"; exit 1; }
done
echo "=== ALL DONE $(date)"
ls -l my_custom_model/
