#!/bin/bash
set -e

MODEL="deepseek-coder-v2:16b-lite-instruct-q4_K_M"
CACHE="pr_cache/"
EXEMPLARS="exemplar_index.json"
CONCURRENCY=3

YEARS=(2016 2017 2018 2019 2020 2021 2022 2023 2024 2025)

for YEAR in "${YEARS[@]}"; do
    INPUT="dataset-${YEAR}.jsonl"
    OUTPUT="evaluations_v3-${YEAR}.jsonl"
    
    if [ ! -f "$INPUT" ]; then
        echo "Skipping $YEAR — $INPUT not found"
        continue
    fi
    
    INPUT_LINES=$(wc -l < "$INPUT")
    OUTPUT_LINES=0
    if [ -f "$OUTPUT" ]; then
        OUTPUT_LINES=$(wc -l < "$OUTPUT")
    fi
    
    if [ "$OUTPUT_LINES" -ge "$INPUT_LINES" ]; then
        echo "Skipping $YEAR — already complete ($OUTPUT_LINES/$INPUT_LINES)"
        continue
    fi
    
    echo "=============================="
    echo "Running $YEAR ($INPUT_LINES projects)"
    echo "=============================="
    
    python3 evaluate_design_v3.py \
        --input "$INPUT" \
        --output "$OUTPUT" \
        --cache "$CACHE" \
        --exemplars "$EXEMPLARS" \
        --model "$MODEL" \
        --concurrency $CONCURRENCY
    
    echo "Done with $YEAR"
done

echo "=============================="
echo "All years complete."
echo "=============================="
