"""Container images as a platform concern: declared registries, the runtime-image policy,
and which tenant an image belongs to.

Builds themselves happen elsewhere -- on builders an operator already governs -- so this
package is about what Pyrrhula is still responsible for whichever builder ran: which
registries exist, which image references a tenant may use, and (in later phases) verifying
and promoting what a builder produced.
"""
