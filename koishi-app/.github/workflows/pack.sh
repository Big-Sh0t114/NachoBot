rm .gitignore

echo "package.json" >> .gitignore
echo "README*" >> .gitignore
echo "LICENSE*" >> .gitignore
echo "yarn.lock" >> .gitignore
echo "node_modules" >> .gitignore
echo ".yarnrc.yml" >> .gitignore
echo "!.git" >> .gitignore
echo "!.yarn" >> .gitignore
echo ".yarn/patches" >> .gitignore
echo ".yarn/plugins" >> .gitignore
echo ".yarn/releases" >> .gitignore
echo ".yarn/sdks" >> .gitignore
echo ".yarn/versions" >> .gitignore

for file in $(cat package.json | jq -r '.files' | sed '1d' | sed '$d'); do
  echo $file | cut -d \" -f 2 >> .gitignore
done

for file in $(find . -type f,l | git check-ignore --stdin --no-index); do
  dir=$(dirname $RUNNER_TEMP/bundle/$file)
  mkdir -p $dir
  cp -a $file $dir
done

cd $RUNNER_TEMP/bundle

zip $([[ $OSTYPE = "msys" ]] && echo "-9qr" || echo "-9qry") ../bundle.zip $(ls -A)
