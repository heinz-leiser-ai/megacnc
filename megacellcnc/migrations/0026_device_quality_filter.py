from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('megacellcnc', '0025_seed_mccpro_chemistries'),
    ]

    operations = [
        migrations.AddField(
            model_name='device',
            name='min_capacity_mah',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='device',
            name='max_esr_mohm',
            field=models.FloatField(default=0),
        ),
    ]
