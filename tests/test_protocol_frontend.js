"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const values = new Map();
const requests = [];
const context = {
    console: {log() {}},
    OCTOPRINT_VIEWMODELS: [],
    $: value => {
        if (typeof value === "function") return value();
        return {text: text => values.set(value, text)};
    },
    OctoPrint: {simpleApiGet(plugin) {
        requests.push(plugin);
        return {done(callback) { callback({protocol: "M9001 Draw v2 (CAPS confirmed)"}); }};
    }}
};
vm.createContext(context);
vm.runInContext(fs.readFileSync("octoprint_LCD_E3V3SE/static/js/LCD_E3V3SE.js", "utf8"), context);
const viewModel = new context.OCTOPRINT_VIEWMODELS[0].construct([{}]);
viewModel.onSettingsShown();
assert.deepEqual(requests, ["LCD_E3V3SE"]);
assert.equal(values.get("#e3v3selcd-protocol-status"), "M9001 Draw v2 (CAPS confirmed)");
viewModel.onDataUpdaterPluginMessage("LCD_E3V3SE", {type: "protocol_status", protocol: "Disconnected"});
assert.equal(values.get("#e3v3selcd-protocol-status"), "Disconnected");
const template = fs.readFileSync("octoprint_LCD_E3V3SE/templates/settings.LCD_E3V3SE.jinja2", "utf8");
assert.ok(template.includes("settings.plugins.LCD_E3V3SE.lcd_protocol"));
assert.ok(template.includes('id="e3v3selcd-protocol-status"'));
console.log("PASS protocol setting, authenticated API wrapper and live connection status");
