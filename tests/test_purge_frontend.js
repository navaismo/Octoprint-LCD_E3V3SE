"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("octoprint_LCD_E3V3SE/static/js/LCD_E3V3SE.js", "utf8");
const start = source.indexOf("self.purgePending = false;");
const end = source.indexOf("\n        };", start) + "\n        };".length;
assert.ok(start >= 0 && end > start);
const requests = [], errors = [];
let disabled = false;
const context = {
    self: {},
    $: () => ({prop: (key, value) => { assert.equal(key, "disabled"); disabled = value; }}),
    showErrorPopup: error => errors.push(error),
    OctoPrint: {simpleApiCommand: (plugin, command, data) => {
        const request = {plugin, command, data};
        requests.push(request);
        return {
            fail(callback) { request.fail = callback; return this; },
            always(callback) { request.always = callback; return this; }
        };
    }}
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
context.self.purge_filament();
assert.equal(disabled, true);
assert.equal(requests.length, 1);
assert.equal(requests[0].plugin, "LCD_E3V3SE");
assert.equal(requests[0].command, "purge_filament");
assert.deepEqual(Object.keys(requests[0].data), []);
context.self.purge_filament();
assert.equal(requests.length, 1, "Repeated clicks must not duplicate the API request");
requests[0].fail({responseJSON: {error: "A purge is already in progress."}});
requests[0].always();
assert.equal(disabled, false);
assert.equal(context.self.purgePending, false);
assert.deepEqual(errors, ["A purge is already in progress."]);
context.self.purge_filament();
assert.equal(requests.length, 2);
requests[1].fail({});
requests[1].always();
assert.equal(errors.at(-1), "Could not purge filament.");
console.log("PASS backend purge API, duplicate click suppression, errors and button restoration");
