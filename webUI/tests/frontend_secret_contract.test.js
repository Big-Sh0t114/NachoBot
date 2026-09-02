'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const setupPath = path.resolve(__dirname, '..', 'static', 'js', 'setup.js');
const source = fs.readFileSync(setupPath, 'utf8');
const tokenInput = {
    value: '',
    classList: {
        _values: new Set(),
        add(value) { this._values.add(value); },
        remove(value) { this._values.delete(value); },
        contains(value) { return this._values.has(value); },
    },
};
let checkedComponents = [];

const documentStub = {
    getElementById(id) {
        return id === 'setup-discord-token' ? tokenInput : null;
    },
    querySelectorAll(selector) {
        if (selector === '.setup-component-cb:checked') return checkedComponents;
        return [];
    },
};

const context = {
    console,
    document: documentStub,
    window: {},
    Promise,
    Error,
    setTimeout,
    clearTimeout,
};
vm.runInNewContext(`${source}\nthis.__setupModule = SetupModule;`, context, {
    filename: setupPath,
});
const contract = context.__setupModule.__test;
assert(contract, 'test-only setup contract is unavailable');

function freshWizardData() {
    return { discord: { token: 'example-value' } };
}

function assertCleared(wizardData) {
    assert.strictEqual(tokenInput.value, '', 'DOM token was not cleared');
    assert(
        !Object.prototype.hasOwnProperty.call(wizardData.discord || {}, 'token'),
        'wizard token was not deleted'
    );
    assert(!tokenInput.classList.contains('input-error'), 'input error was not cleared');
}

async function main() {
    checkedComponents = [{ value: 'discord' }];
    contract.onComponentToggle();
    tokenInput.value = 'example-value';
    const selected = contract.collectWizardData();
    assert.strictEqual(selected.discord.token, 'example-value');

    const activeWizardData = freshWizardData();
    tokenInput.value = 'example-value';
    await contract.runGitBootstrapAttempt(activeWizardData, async () => ({ status: 'ok' }));
    tokenInput.classList.add('input-error');
    checkedComponents = [];
    contract.onComponentToggle();
    const deselected = contract.collectWizardData();
    assert(!Object.prototype.hasOwnProperty.call(deselected, 'discord'));
    assertCleared(activeWizardData);

    tokenInput.value = 'example-value';
    const returnedGitFailure = freshWizardData();
    const returned = await contract.runGitBootstrapAttempt(
        returnedGitFailure,
        async () => ({ status: 'error' })
    );
    assert.strictEqual(returned.status, 'error');
    assertCleared(returnedGitFailure);

    tokenInput.value = 'example-value';
    const thrownGitFailure = freshWizardData();
    await assert.rejects(
        contract.runGitBootstrapAttempt(thrownGitFailure, async () => {
            throw new Error('transport failure');
        }),
        /transport failure/
    );
    assertCleared(thrownGitFailure);

    const configOutcomes = [
        { result: { status: 'ok' } },
        { result: { errors: ['configuration rejected'] } },
        { error: new Error('configuration transport failure') },
    ];
    for (const outcome of configOutcomes) {
        tokenInput.value = 'example-value';
        const wizardData = freshWizardData();
        let observedToken = null;
        const request = async data => {
            observedToken = data.discord.token;
            if (outcome.error) throw outcome.error;
            return outcome.result;
        };
        if (outcome.error) {
            await assert.rejects(contract.runDiscordConfigAttempt(wizardData, request));
        } else {
            await contract.runDiscordConfigAttempt(wizardData, request);
        }
        assert.strictEqual(observedToken, 'example-value');
        assertCleared(wizardData);
    }
}

main().then(
    () => process.stdout.write('frontend secret contract: ok\n'),
    error => {
        process.stderr.write('frontend secret contract failed\n');
        process.exitCode = 1;
    }
);
