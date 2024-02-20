#!/bin/bash
year=$(echo "$1" | cut -d "-" -f 1)
mkdir -p "$year"
if [ ! -f "TheEconomist-$1-$2.pdf" ];then

        sed  "s/edition_date = .*/edition_date = '$1'/" economist.recipe > "TheEconomist-$1-$2.recipe"

        #ebook-convert "TheEconomist-$1-$2.recipe" .mobi --output-profile=kindle_oasis --pubdate="$1" -vv --mobi-file-type=new --authors="TheEconomist" --title="TheEconomist-$1-$2"
	ebook-convert "TheEconomist-$1-$2.recipe" .pdf --paper-size=a4 --pdf-page-numbers --pdf-page-margin-bottom=42 --pdf-page-margin-top=42 --pdf-page-margin-left=42 --pdf-page-margin-right=42 -vv --title="TheEconomist-$1-$2.pdf"
        rm "TheEconomist-$1-$2.recipe"
        mv "TheEconomist-$1-$2.pdf" pdf/"$year"
fi
