require('esbuild').build({
  entryPoints: ['src/extension.ts'],
  bundle: true,
  format: 'cjs',
  platform: 'node',
  outfile: 'dist/extension.js',
  external: ['vscode'],
  minify: process.argv.includes('--production'),
  sourcemap: !process.argv.includes('--production'),
}).catch((e) => { console.error(e); process.exit(1); });
