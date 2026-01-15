variant=$1

name=$(cat package.json | jq -r '.name' | cut -d / -f 2)
tag_name=$(gh release view --json name --jq .name)
upload_file=$RUNNER_TEMP/$name-$tag_name-$variant.zip

echo name: $name
echo tag_name: $tag_name
echo upload_file: $upload_file

mv $RUNNER_TEMP/bundle.zip $upload_file

gh release upload $tag_name $upload_file
