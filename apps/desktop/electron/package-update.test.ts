import assert from 'node:assert/strict'

import { test } from 'vitest'

import { resolvePackageUpdate } from './package-update'

test('resolvePackageUpdate returns the Flatpak update command', () => {
  assert.deepEqual(resolvePackageUpdate({ HERMES_DESKTOP_FLATPAK: '1' }), {
    command: 'flatpak update --user com.nousresearch.Hermes',
    kind: 'flatpak',
    message: 'This Flatpak is updated through Flatpak.',
    reason: 'flatpak-managed'
  })
})

test('resolvePackageUpdate returns the Snap refresh command', () => {
  assert.deepEqual(resolvePackageUpdate({ HERMES_DESKTOP_SNAP: '1' }), {
    command: 'sudo snap refresh hermes-desktop',
    kind: 'snap',
    message: 'This Snap is updated through Snap.',
    reason: 'snap-managed'
  })
})

test('resolvePackageUpdate keeps source installs on the in-app updater', () => {
  assert.equal(resolvePackageUpdate({}), null)
})
