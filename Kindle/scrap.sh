#!/bin/bash
year=$(echo "$1" | cut -d "-" -f 1)
mkdir -p "$year"
if [ ! -f "TheEconomist-$1-$2.mobi" ];then

        sed  "s/edition_date = .*/edition_date = '$1'/" economist.recipe > "TheEconomist-$1-$2.recipe"

        ebook-convert "TheEconomist-$1-$2.recipe" .mobi --output-profile=kindle_oasis --pubdate="$1" -vv --mobi-file-type=new --authors="TheEconomist" --title="TheEconomist-$1-$2"

        rm "TheEconomist-$1-$2.recipe"
        mv "TheEconomist-$1-$2.mobi" "$year"
fi
