#!/bin/bash
# Run on the operator machine with a completed, staged campaign directory.
set -euo pipefail
if [[ $# != 5 ]]; then
    echo "Usage: bash archive_and_stop.sh PROFILE REGION INSTANCE_ID RESULTS_DIR S3_PREFIX" >&2
    exit 2
fi
profile=$1 region=$2 instance=$3 results=$4 prefix=$5
[[ $prefix == s3://* && -d $results && -n $(find "$results" -type f -print -quit) ]]
aws_cli=(aws --profile "$profile" --region "$region")
project=$("${aws_cli[@]}" ec2 describe-instances --instance-ids "$instance" --query 'Reservations[0].Instances[0].Tags[?Key==`Project`].Value | [0]' --output text)
[[ $project == pmt ]]
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
tar -C "$results" -czf "$work/results.tar.gz" .
digest=$(sha256sum "$work/results.tar.gz" | cut -d' ' -f1)
object="${prefix%/}/$instance/$digest.tar.gz"
"${aws_cli[@]}" s3 cp "$work/results.tar.gz" "$object" --only-show-errors
"${aws_cli[@]}" s3 cp "$object" "$work/readback.tar.gz" --only-show-errors
cmp "$work/results.tar.gz" "$work/readback.tar.gz"
echo "Verified campaign archive $object"
"${aws_cli[@]}" ec2 stop-instances --instance-ids "$instance"
"${aws_cli[@]}" ec2 wait instance-stopped --instance-ids "$instance"
