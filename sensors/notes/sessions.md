# Capture sessions

## Checklist

1. Moisten the electrodes, put the strap on. Close Polar Flow / Beat on the phone.
2. `python record.py --subject <same id you pick in the launcher>`
3. Sit still for two minutes — this becomes the baseline.
4. `python ../app/run_study.py`. Pick the participant and the warm-up, launch,
   and play. Answer "Submit this run?" after each run. Change the warm-up
   dropdown whenever the condition changes — each run is stamped with whatever
   is selected when it starts. Press `m` in the recorder window to mark anything
   worth noting.
5. Quit FPSci through its menu, then press `q` in the recorder window.
6. `python correlate.py`, then add a line below.
7. Check `session.json`: `errors` empty, `effective_rates_hz.ecg` near 130.
8. Check `analysis/_index/runs.csv`: every run you meant to keep says
   `submitted = yes` and carries the right warm-up label.

## Log

| Date (UTC) | Recording id | Subject | Duration | Runs | Warm-up | Notes |
|------------|--------------|---------|----------|------|---------|-------|
