#!/bin/bash
# Build the Svelte frontend and copy to app/static

set -e

echo "Installing dependencies..."
npm install

echo "Building frontend..."
npm run build

echo "Copying build to app/static..."
rm -rf ../app/static
cp -r build ../app/static

echo "Done! Frontend built and copied to app/static"
