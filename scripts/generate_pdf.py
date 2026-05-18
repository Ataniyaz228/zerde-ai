import os
import urllib.request
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER

def generate_pdf():
    font_path = "/usr/share/fonts/TTF/DejaVuSans.ttf"
    pdfmetrics.registerFont(TTFont('DejaVuSans', font_path))
    
    font_bold_path = "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
    pdfmetrics.registerFont(TTFont('DejaVuSans-Bold', font_bold_path))

    doc = SimpleDocTemplate("docs/analytical_report.pdf", pagesize=A4,
                            rightMargin=72, leftMargin=72,
                            topMargin=72, bottomMargin=18)

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Justify', alignment=TA_JUSTIFY, fontName='DejaVuSans', fontSize=11, leading=14))
    styles.add(ParagraphStyle(name='TitleCyrillic', alignment=TA_CENTER, fontName='DejaVuSans-Bold', fontSize=14, leading=18, spaceAfter=20))
    styles.add(ParagraphStyle(name='HeadingCyrillic', alignment=TA_JUSTIFY, fontName='DejaVuSans-Bold', fontSize=12, leading=16, spaceBefore=15, spaceAfter=10))

    Story = []

    # Title
    Story.append(Paragraph("АНАЛИТИЧЕСКАЯ СПРАВКА", styles['TitleCyrillic']))
    Story.append(Paragraph("к проекту Закона Республики Казахстан «О внесении изменений и дополнений в некоторые законодательные акты Республики Казахстан по вопросам цифровизации и защиты персональных данных»", styles['TitleCyrillic']))
    Story.append(Spacer(1, 12))

    # Introduction (Water/Fluff)
    Story.append(Paragraph("1. Введение и общие положения", styles['HeadingCyrillic']))
    intro_text = """В свете реализации Государственной программы «Цифровой Казахстан» и в соответствии с 
    Посланием Главы государства народу Казахстана, перед государственными органами поставлена масштабная задача по 
    максимальному переводу взаимодействия граждан с государством и бизнесом в электронный формат. В условиях 
    глобализации и стремительного развития информационных технологий, обеспечение надлежащего уровня защищенности 
    персональных данных граждан Республики Казахстан является одним из краеугольных камней национальной безопасности."""
    Story.append(Paragraph(intro_text, styles['Justify']))
    Story.append(Spacer(1, 12))
    
    fluff_text = """В настоящее время наблюдается устойчивая тенденция роста числа правонарушений в сфере информатизации, 
    что связано с бурным развитием цифровых сервисов, электронной коммерции и трансграничного обмена данными. 
    Существующая нормативно-правовая база, несмотря на свою базовую эффективность, в ряде аспектов не успевает за 
    стремительной эволюцией технологического уклада. Это требует принятия своевременных, взвешенных и комплексных 
    законодательных мер, направленных на минимизацию рисков утечки персональных данных и повышение доверия 
    граждан к цифровым институтам."""
    Story.append(Paragraph(fluff_text, styles['Justify']))
    Story.append(Spacer(1, 12))

    # Core Proposals
    Story.append(Paragraph("2. Основные концептуальные изменения", styles['HeadingCyrillic']))
    core_text_1 = """Настоящим законопроектом предлагается внесение изменений в Закон Республики Казахстан 
    от 21 мая 2013 года «О персональных данных и их защите». В частности, пункт 1 статьи 16 предлагается дополнить 
    подпунктом 4) следующего содержания: «4) оператор обязан уведомить уполномоченный орган об инциденте 
    информационной безопасности, повлекшем незаконный доступ к персональным данным, в течение 24 часов с момента 
    обнаружения такого инцидента»."""
    Story.append(Paragraph(core_text_1, styles['Justify']))
    Story.append(Spacer(1, 12))

    core_text_2 = """Кроме того, в целях усиления контроля, предлагается дополнить Кодекс Республики Казахстан 
    об административных правонарушениях новой статьей 79-1, предусматривающей административный штраф 
    для субъектов крупного предпринимательства в размере 10 000 месячных расчетных показателей (МРП) за сокрытие 
    факта утечки персональных данных."""
    Story.append(Paragraph(core_text_2, styles['Justify']))
    Story.append(Spacer(1, 12))

    # More fluff / justifications
    Story.append(Paragraph("3. Ожидаемые социально-экономические последствия", styles['HeadingCyrillic']))
    fluff_2 = """Принятие рассматриваемого законопроекта не повлечет негативных социально-экономических 
    и правовых последствий. Напротив, закрепление жестких сроков уведомления об инцидентах кибербезопасности 
    создаст правовую основу для оперативного реагирования уполномоченных органов на угрозы, что в конечном итоге 
    существенно повысит индекс защиты прав человека в киберпространстве. Реализация законопроекта не потребует 
    выделения дополнительных финансовых средств из республиканского бюджета."""
    Story.append(Paragraph(fluff_2, styles['Justify']))
    Story.append(Spacer(1, 12))

    conclusion = """Резюмируя вышеизложенное, концептуальные положения законопроекта полностью коррелируют с 
    целями устойчивого развития и передовыми международными стандартами, включая положения Общего регламента по 
    защите данных (GDPR) Европейского Союза."""
    Story.append(Paragraph(conclusion, styles['Justify']))
    
    # Signatures
    Story.append(Spacer(1, 40))
    Story.append(Paragraph("Министр цифрового развития, инноваций", styles['Justify']))
    Story.append(Paragraph("и аэрокосмической промышленности РК", styles['Justify']))
    Story.append(Spacer(1, 20))
    Story.append(Paragraph("_________________ / Подпись /", styles['Justify']))

    doc.build(Story)
    print("PDF generated at docs/analytical_report.pdf")

if __name__ == "__main__":
    generate_pdf()
