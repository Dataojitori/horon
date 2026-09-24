#!/bin/sh
# 用法: sh sheet.sh name t1,t2,...(12个)
cd "$(dirname "$0")"
rm -rf tmp_s
node render.js --times "$2" tmp_s >/dev/null || { echo "render.js 渲染失败" >&2; exit 1; }
ls tmp_s/*.jpg >/dev/null 2>&1 || { echo "没有渲染出任何帧" >&2; exit 1; }
ls tmp_s/*.jpg | sort -t_ -k3 -g > tmp_s/list.txt
i=0; args=""; for f in $(cat tmp_s/list.txt); do args="$args -i $f"; i=$((i+1)); done
n=$i; cols=4; rows=$(( (n+cols-1)/cols ))
layout=""; for k in $(seq 0 $((n-1))); do x=$(( (k%cols)*480 )); y=$(( (k/cols)*270 )); layout="$layout${layout:+|}${x}_${y}"; done
f=""; for k in $(seq 0 $((n-1))); do f="$f[$k:v]scale=480:270[v$k];"; done
ins=""; for k in $(seq 0 $((n-1))); do ins="$ins[v$k]"; done
ffmpeg -y -loglevel error $args -filter_complex "${f}${ins}xstack=inputs=$n:layout=$layout:fill=black" "sheet_$1.jpg"
