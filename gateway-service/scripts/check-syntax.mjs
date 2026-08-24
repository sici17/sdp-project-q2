/**
 * Parse every module under src/, so adding one cannot skip the check.
 *
 * `npm run check` used to name six files by hand. The gateway has nine, and the
 * three it never named included access.js - the tenancy and visibility model
 * the whole permissions story rests on. A list maintained by hand fails
 * silently, and it fails on exactly the file nobody remembers to add.
 */

import { execFileSync } from 'node:child_process'
import { readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const srcDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'src')
const modules = readdirSync(srcDir)
  .filter((name) => name.endsWith('.js'))
  .sort()

if (modules.length === 0) {
  console.error(`No modules found in ${srcDir}`)
  process.exit(1)
}

for (const name of modules) {
  execFileSync(process.execPath, ['--check', join(srcDir, name)], { stdio: 'inherit' })
}

console.log(`Parsed ${modules.length} modules.`)
