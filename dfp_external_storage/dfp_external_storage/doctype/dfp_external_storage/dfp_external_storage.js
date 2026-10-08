
frappe.ui.form.on('DFP External Storage', {

	setup: frm => {
		frm.button_remote_files_list = null
	},

	refresh: async function(frm) {
		if (frm.is_new() && !frm.doc.doctypes_ignored.length) {
			frm.doc.doctypes_ignored.push({doctype_to_ignore: 'Data Import'})
			frm.doc.doctypes_ignored.push({doctype_to_ignore: 'Prepared Report'})
			frm.refresh_field('doctypes_ignored')
		}

		if (frm.doc.enabled) {
			frm.button_remote_files_list = frm.add_custom_button(
				__('List files in bucket'),
				() => frappe.set_route('dfp-s3-bucket-list', frm.doc.name)
				// () => frappe.set_route('dfp-s3-bucket-list', { storage: frm.doc.name })
			)
		}

		frm.set_query('folders', function() {
			return {
				filters: {
					is_folder: 1,
				},
			}
		})

		const current_storage = frm.doc.name
		const page_length = 100
		const assigned_folders = []
		let limit_start = 0
		while (true) {
			const rows = await frappe.db.get_list('DFP External Storage by Folder', {
				fields: ['parent', 'folder'],
				parent_doctype: 'DFP External Storage',
				filters: { parenttype: 'DFP External Storage' },
				order_by: 'name asc',
				limit_start,
				limit: page_length,
			})
			assigned_folders.push(...rows.filter(row => row.parent !== current_storage).map(row => row.folder))
			if (rows.length < page_length) break
			limit_start += page_length
		}
		// Ignore a response if navigation switched to a different storage form.
		if (frm.doc.name !== current_storage) return
		frm.set_query('folders', () => ({
			filters: {
				is_folder: 1,
				...(assigned_folders.length ? { name: ['not in', [...new Set(assigned_folders)]] } : {}),
			},
		}))

	},

})
