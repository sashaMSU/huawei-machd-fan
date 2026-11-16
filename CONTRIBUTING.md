# Contributing / вклад в проект

Keep the runtime small: one Python file, standard library, no GUI, no host-side
fan loop. Explain any extra background process, dependency or state file.

Перед PR выполните:

```bash
python3 huawei_fan_table.py check
python3 -m unittest discover -s tests -v
bash -n install.sh uninstall.sh
```

Для исправления протокола или карты памяти нужны исходные доказательства:
версия BIOS/EC, схема ACPI-доступа, независимое подтверждение назначения поля,
прочитанные начальные значения и проверенный способ возврата. Совпадения названия
ноутбука или GUID прошивки недостаточно. Не принимайте новую плату только путём
расширения DMI allowlist.

Разделяйте:
- факты из снимка конфигурации;
- интерпретацию кода прошивки;
- результаты модели;
- реальные аппаратные наблюдения.

Для изменения поведения добавьте тест. Модель памяти проверяет работу программы,
но не доказывает безопасность температур или корректность физического охлаждения.
Не публикуйте дампы BIOS, драйверы, серийные номера, пароли и личные журналы.
Ненулевые I/D требуют отдельного разбора накопленного состояния и условий его сброса.

Comments and documentation may be Russian or English. Keep the two README files
consistent. No claim of compatibility, safety, fan-noise reduction or unchanged
performance should be made without stating what was actually measured.

Contributions are provided under the repository's MIT license.
