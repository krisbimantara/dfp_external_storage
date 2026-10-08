const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname,
    '../dfp_external_storage/dfp_external_storage/doctype/dfp_external_storage/dfp_external_storage.js'), 'utf8');

function formWithPages(pages) {
    let handlers;
    const requests = [];
    const queries = [];
    const frappe = {
        ui: { form: { on: (doctype, events) => { handlers = events; } } },
        db: { get_list: async (doctype, args) => {
            requests.push({ doctype, args });
            return pages[requests.length - 1];
        } },
    };
    vm.runInNewContext(source, { frappe, __: value => value });
    const form = {
        doc: { name: 'storage-a', enabled: 0 },
        is_new: () => false,
        set_query: (field, query) => { queries.push(query); },
    };
    return { form, handlers, requests, queries };
}

test('folder selector paginates beyond the first page and keeps this storage folders selectable', async () => {
    const firstPage = Array.from({ length: 100 }, (_, index) => ({ parent: 'storage-b', folder: `Other/${index}` }));
    const scenario = formWithPages([firstPage, [
        { parent: 'storage-a', folder: 'Own' },
        { parent: 'storage-b', folder: 'BeyondFirstPage' },
    ]]);
    await scenario.handlers.refresh(scenario.form);
    assert.equal(scenario.requests.length, 2);
    assert.equal(scenario.requests[0].args.parent_doctype, 'DFP External Storage');
    assert.equal(scenario.requests[0].args.limit_start, 0);
    assert.equal(scenario.requests[1].args.limit_start, 100);
    const filters = scenario.queries.at(-1)().filters;
    assert.equal(filters.is_folder, 1);
    assert.equal(filters.name[0], 'not in');
    assert.equal(filters.name[1].length, 101);
    assert.ok(filters.name[1].includes('BeyondFirstPage'));
    assert.ok(!filters.name[1].includes('Own'));
});

test('no assigned folders leaves the folder query without an empty exclusion', async () => {
    const scenario = formWithPages([[]]);
    await scenario.handlers.refresh(scenario.form);
    assert.equal(scenario.requests.length, 1);
    assert.equal(scenario.queries.at(-1)().filters.name, undefined);
});
