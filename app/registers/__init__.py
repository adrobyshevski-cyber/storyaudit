"""Register services for report_v2.

Imports stay explicit (`app.registers.codes`, `app.registers.sources`) because
`app.models` imports `app.registers.models` for metadata registration, and a
package-level re-export would create an import cycle.
"""
