# Rolling portfolio update

This version replaces the monthly upload workflow in the Admin Panel with a
managed holding lifecycle.

## Before running

Back up the database, then apply the migration from the project directory:

```powershell
flask --app app db upgrade
```

The migration adds optional company name, quantity, notes and sale price fields.
It does not delete or rewrite existing holdings.

## Behaviour

- Existing unsold holdings remain visible, even when they came from an older
  monthly upload.
- New holdings are stored in a permanent rolling portfolio.
- A holding remains on the dashboard until an administrator records its sale.
- Sold holdings are removed from the live dashboard and retained in Portfolio
  History with their realised return.
- `Delete error` permanently removes a mistaken record and should not be used
  for a genuine sale.
- The live dashboard remains equal weighted. Quantity is captured for later
  value-weighted reporting but does not change the current calculation.

The legacy `/admin/upload` endpoint remains in the code for backward
compatibility, but it is no longer exposed in the Admin Panel.
